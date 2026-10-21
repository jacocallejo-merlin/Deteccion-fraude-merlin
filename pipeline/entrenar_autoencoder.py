import argparse
import json
import os
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import average_precision_score
from torch import nn
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import CONFIG, fijar_semillas, CARPETA_DATOS, CARPETA_MODELOS
from artefactos import crear_carpeta_run, guardar as guardar_artefacto
from modelos_nosupervisados import (conectar_clickhouse, guardar_en_clickhouse,
                                    normalizar, percentil, referencia)

MODELO = 'autoencoder'

# Los VALORES están en config.yaml (sección "autoencoder"); aquí se explica para qué sirve cada uno.
_cfg = CONFIG['autoencoder']

# Máximo de épocas: casi nunca se llega, la parada temprana corta antes.
EPOCAS = _cfg['epocas']
BATCH = _cfg['batch']
# Épocas seguidas sin mejorar la métrica de validación antes de parar.
PACIENCIA = _cfg['paciencia']
# Criterio de parada: 'loss' (error de reconstrucción) o 'prauc' (calidad del ranking de fraude).
SELECCION = _cfg.get('seleccion', 'loss')
# Cada cuántas épocas se mide la PR-AUC de validación (1 = todas; con 10.000 filas es barato).
EVAL_CADA = _cfg.get('eval_cada', 1)
# Igual que los clásicos: la validación tiene que ser exactamente las mismas filas.
VALIDACION = CONFIG['modelos']['proporcion_val']
SEMILLA = CONFIG['semilla']
# Se reentrena con estas semillas para ver si el resultado depende del azar.
SEMILLAS_ESTABILIDAD = _cfg['semillas_estabilidad']
# Configuración que se entrena. Para probar otras: pipeline/buscar_autoencoder.py
PARAMETROS = {'capas': list(_cfg['capas']), 'cuello': _cfg['cuello'],
              'activacion': _cfg.get('activacion', 'tanh'),
              'dropout': _cfg.get('dropout', 0.0),
              'batchnorm': _cfg.get('batchnorm', False),
              'ruido': _cfg['ruido'], 'lr': _cfg['lr']}

ACTIVACIONES = {'tanh': nn.Tanh, 'relu': nn.ReLU,
                'leaky': lambda: nn.LeakyReLU(negative_slope=0.01),
                'gelu': nn.GELU, 'elu': nn.ELU, 'silu': nn.SiLU}


def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))
    if not X_train.index.equals(info_train.index):
        raise ValueError('X_train e info_train no tienen las mismas filas. '
                         'Vuelve a ejecutar preparacion_datos_ml.py')
    print(f'Train: {X_train.shape}   Test: {X_test.shape}')

    corte = int(len(X_train) * (1 - VALIDACION))
    es_fraude = info_train['es_fraude'].astype(int).to_numpy()
    legitimas = es_fraude == 0

    X_fit = X_train.iloc[:corte][legitimas[:corte]]
    X_val = X_train.iloc[corte:][legitimas[corte:]]
    X_val_completo = X_train.iloc[corte:]
    y_val = es_fraude[corte:]
    if y_val.sum() == 0:
        raise ValueError('No hay fraudes en validación: no se puede elegir configuración')

    print(f'Entrenamiento semi-supervisado: solo transacciones legítimas '
          f'({es_fraude.sum()} fraudes apartados)')
    print(f'  ajuste: {len(X_fit):,} filas   validación: {len(X_val):,} filas (las más recientes)')
    print(f'  validación completa para elegir: {len(X_val_completo):,} filas '
          f'({int(y_val.sum())} fraudes)')
    ids = pd.concat([info_train['transaccion_id'], info_test['transaccion_id']])
    return X_train, X_test, X_fit, X_val, X_val_completo, y_val, ids


class Autoencoder(nn.Module):
    def __init__(self, n_entrada, capas, cuello, activacion='tanh',
                 dropout=0.0, batchnorm=False):
        super().__init__()
        if activacion not in ACTIVACIONES:
            raise ValueError(f'Activación desconocida: {activacion}')
        tamanos = [n_entrada] + list(capas) + [cuello]
        # Cada capa tiene que comprimir: una capa igual o más ancha que la anterior
        # (p. ej. capas [24, 16, 8] con cuello 8 -> 8 -> 8) no aporta nada y solo añade parámetros.
        if any(b >= a for a, b in zip(tamanos[:-1], tamanos[1:])):
            raise ValueError(f'Arquitectura no válida {tamanos}: cada capa del codificador '
                             f'tiene que ser más estrecha que la anterior')

        codificador = []
        for a, b in zip(tamanos[:-1], tamanos[1:]):
            codificador.append(nn.Linear(a, b))
            if batchnorm:
                codificador.append(nn.BatchNorm1d(b))
            codificador.append(ACTIVACIONES[activacion]())
            if dropout:
                codificador.append(nn.Dropout(dropout))
        self.codificador = nn.Sequential(*codificador)

        invertido = tamanos[::-1]
        decodificador = []
        for i, (a, b) in enumerate(zip(invertido[:-1], invertido[1:])):
            decodificador.append(nn.Linear(a, b))
            if i < len(invertido) - 2:
                if batchnorm:
                    decodificador.append(nn.BatchNorm1d(b))
                decodificador.append(ACTIVACIONES[activacion]())
                if dropout:
                    decodificador.append(nn.Dropout(dropout))
        self.decodificador = nn.Sequential(*decodificador)

    def forward(self, x):
        return self.decodificador(self.codificador(x))


def describir(n_entrada, p):
    return ' -> '.join(str(t) for t in
                       [n_entrada, *p['capas'], p['cuello'], *p['capas'][::-1], n_entrada])


def entrenar(X_fit, X_val, p, semilla, detalle=True,
             X_val_completo=None, y_val=None):
    """Entrena con los parámetros p (capas, cuello, activación, ruido, lr...).

    La parada temprana puede usar la pérdida de validación ('loss') o la PR-AUC de
    validación ('prauc'). La segunda es la que se usa en producción: mide directamente
    lo que importa (que el fraude quede arriba), no solo que la red copie bien los pagos.
    Devuelve el modelo con el mejor estado y un resumen del entrenamiento."""
    torch.manual_seed(semilla)
    np.random.seed(semilla)
    # Red muy pequeña y lotes de 1024: con varios hilos se pierde más tiempo repartiendo el trabajo
    # que calculando (medido: 3,3 s/época con 10 hilos frente a 0,6 s con 1).
    torch.set_num_threads(1)

    datos = torch.tensor(X_fit.to_numpy(), dtype=torch.float32)
    validacion = torch.tensor(X_val.to_numpy(), dtype=torch.float32)
    ruido = p.get('ruido', 0.0)

    usar_prauc = SELECCION == 'prauc'
    validacion_completa = None
    y_val_arr = None
    if X_val_completo is not None:
        validacion_completa = torch.tensor(X_val_completo.to_numpy(), dtype=torch.float32)
    if y_val is not None:
        y_val_arr = np.asarray(y_val, dtype=int)
    if usar_prauc:
        if validacion_completa is None or y_val_arr is None:
            raise ValueError("La selección 'prauc' necesita X_val_completo e y_val")
        if y_val_arr.sum() == 0 or y_val_arr.sum() == len(y_val_arr):
            raise ValueError('La validación necesita las dos clases para calcular la PR-AUC')

    modelo = Autoencoder(datos.shape[1], p['capas'], p['cuello'],
                         activacion=p.get('activacion', 'tanh'),
                         dropout=p.get('dropout', 0.0),
                         batchnorm=p.get('batchnorm', False))
    if detalle:
        parametros = sum(t.numel() for t in modelo.parameters())
        print(f'\nArquitectura: {describir(datos.shape[1], p)}   ({parametros:,} parámetros)')
        print(f'Activación: {p.get("activacion", "tanh")}   ruido: sigma = {ruido}   '
              f'lr = {p["lr"]}   parada por: {SELECCION}')
        print(f'\n{"época":>6}{"pérdida ajuste":>17}{"pérdida validación":>21}{"PR-AUC val":>14}')

    optimizador = torch.optim.Adam(modelo.parameters(), lr=p['lr'])
    criterio = nn.MSELoss()
    generador = torch.Generator().manual_seed(semilla)

    mejor_loss, mejor_estado_loss, mejor_epoca_loss, sin_mejorar_loss = float('inf'), None, 0, 0
    mejor_ap, mejor_estado_ap, mejor_epoca_ap = -1.0, None, 0
    for epoca in range(1, EPOCAS + 1):
        modelo.train()
        orden = torch.randperm(len(datos), generator=generador)
        acumulado = 0.0
        for i in range(0, len(datos), BATCH):
            lote = datos[orden[i:i + BATCH]]
            ruidoso = lote + torch.randn(lote.shape, generator=generador) * ruido
            perdida = criterio(modelo(ruidoso), lote)
            optimizador.zero_grad()
            perdida.backward()
            optimizador.step()
            acumulado += perdida.item() * len(lote)
        perdida_ajuste = acumulado / len(datos)

        # La validación se mide SIN ruido: es como se puntúa después, y con ruido la pérdida
        # oscilaría por azar y la parada temprana cortaría en una época cualquiera.
        modelo.eval()
        with torch.no_grad():
            perdida_val = criterio(modelo(validacion), validacion).item()

        if perdida_val < mejor_loss - 1e-5:
            mejor_loss, mejor_epoca_loss, sin_mejorar_loss = perdida_val, epoca, 0
            mejor_estado_loss = {k: v.clone() for k, v in modelo.state_dict().items()}
        else:
            sin_mejorar_loss += 1

        ap = None
        if usar_prauc and (epoca == 1 or epoca % EVAL_CADA == 0):
            ap = average_precision_score(
                y_val_arr, error_reconstruccion(modelo, validacion_completa))
            if ap > mejor_ap + 1e-7:
                mejor_ap, mejor_epoca_ap = ap, epoca
                mejor_estado_ap = {k: v.clone() for k, v in modelo.state_dict().items()}

        if detalle and (epoca % 50 == 0 or epoca == 1):
            print(f'{epoca:>6}{perdida_ajuste:>17.5f}{perdida_val:>21.5f}'
                  + (f'{ap:>14.4f}' if ap is not None else ''))

        if SELECCION == 'loss':
            if sin_mejorar_loss >= PACIENCIA:
                break
        elif SELECCION == 'prauc':
            if epoca - mejor_epoca_ap >= PACIENCIA:
                break
        else:
            raise ValueError(f"Selección desconocida: {SELECCION}")

    if SELECCION == 'loss':
        estado, mejor_epoca = mejor_estado_loss, mejor_epoca_loss
        metrica = mejor_loss
    else:
        estado, mejor_epoca = mejor_estado_ap, mejor_epoca_ap
        metrica = mejor_ap
    if estado is None:
        raise RuntimeError('La métrica de validación nunca fue finita (¿NaN en los datos?)')
    modelo.load_state_dict(estado)

    # Pérdida y PR-AUC del estado que se guarda (pueden no ser las mejores de cada criterio).
    modelo.eval()
    with torch.no_grad():
        perdida_elegida = criterio(modelo(validacion), validacion).item()
    ap_elegida = None
    if validacion_completa is not None and y_val_arr is not None:
        ap_elegida = float(average_precision_score(
            y_val_arr, error_reconstruccion(modelo, validacion_completa)))

    if detalle:
        print(f'{epoca:>6}   parada temprana. Mejor {SELECCION}: {metrica:.5f} '
              f'(época {mejor_epoca})')
        print(f'  estado guardado: pérdida val {perdida_elegida:.5f}'
              + (f'   PR-AUC val {ap_elegida:.4f}' if ap_elegida is not None else ''))

    info = {
        'perdida_val': float(perdida_elegida),
        'mejor_epoca': mejor_epoca,
        'epocas': epoca,
        'seleccion': SELECCION,
        'mejor_epoca_loss': mejor_epoca_loss,
        'perdida_val_loss': float(mejor_loss),
        'pr_auc_val': ap_elegida,
        'epoca_pr_auc': mejor_epoca_ap if usar_prauc else None,
        'pr_auc_val_ep': float(mejor_ap) if usar_prauc else None,
    }
    return modelo, info


def error_reconstruccion(modelo, X):
    modelo.eval()
    with torch.no_grad():
        # Acepta un DataFrame (lo normal) o un tensor ya preparado (la validación
        # completa que se usa en cada época para calcular la PR-AUC).
        datos = (torch.tensor(X.to_numpy(), dtype=torch.float32)
                 if hasattr(X, 'to_numpy') else X.float())
        return ((modelo(datos) - datos) ** 2).mean(dim=1).numpy()


def error_por_variable(modelo, X):
    modelo.eval()
    with torch.no_grad():
        datos = torch.tensor(X.to_numpy(), dtype=torch.float32)
        return pd.DataFrame(((modelo(datos) - datos) ** 2).numpy(),
                            columns=X.columns, index=X.index)


def estabilidad(p, X_fit, X_val, X_val_completo, y_val):
    if not SEMILLAS_ESTABILIDAD:
        return None, None
    aps = []
    for semilla in SEMILLAS_ESTABILIDAD:
        modelo, _ = entrenar(X_fit, X_val, p, semilla, detalle=False,
                             X_val_completo=X_val_completo, y_val=y_val)
        aps.append(average_precision_score(y_val, error_reconstruccion(modelo, X_val_completo)))
    media, std = float(np.mean(aps)), float(np.std(aps))
    print(f'Estabilidad con {len(aps)} semillas: PR-AUC val {media:.4f} ± {std:.4f}')
    return media, std


def explicar(modelo, X_test, err_test, n=5, variables=3):
    detalle = error_por_variable(modelo, X_test)
    print(f'\nLas {n} transacciones más anómalas del test y por qué:')
    for pos in np.argsort(-err_test)[:n]:
        fila = detalle.iloc[pos].sort_values(ascending=False)
        total = fila.sum()
        culpables = ', '.join(f'{nombre} ({valor / total * 100:.0f} %)'
                              for nombre, valor in fila.head(variables).items())
        print(f'  fila {X_test.index[pos]}  error {err_test[pos]:.2f}  ->  {culpables}')


def guardar(modelo, p, conjuntos, errores, ref, ref_ordenada, n_entrada, columnas):
    """Mismo formato que los clásicos: score = error de reconstrucción (más alto = más anómalo),
    score_norm = 0 típica / 1 percentil 99 del ajuste, score_pct = fracción del ajuste por debajo."""
    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    torch.save({'estado': modelo.state_dict(), 'capas': p['capas'], 'cuello': p['cuello'],
                'activacion': p.get('activacion', 'tanh'),
                'dropout': p.get('dropout', 0.0),
                'batchnorm': p.get('batchnorm', False),
                'seleccion': SELECCION,
                'ruido': p['ruido'], 'lr': p['lr'], 'n_entrada': n_entrada,
                'columnas': columnas},
               os.path.join(CARPETA_MODELOS, f'{MODELO}.pt'))
    joblib.dump(ref_ordenada, os.path.join(CARPETA_MODELOS, f'{MODELO}_referencia.joblib'))

    scores = {}
    for parte in ('train', 'val', 'test'):
        e = errores[parte]
        scores[parte] = pd.DataFrame({'error': e, 'score': e,
                                      'score_norm': normalizar(e, ref['mediana'], ref['p99']),
                                      'score_pct': percentil(e, ref_ordenada)},
                                     index=conjuntos[parte].index)
        scores[parte].to_parquet(os.path.join(CARPETA_MODELOS, f'{MODELO}_scores_{parte}.parquet'))

    print(f'\nGuardado en "{CARPETA_MODELOS}/": {MODELO}.pt, {MODELO}_referencia.joblib '
          'y los scores de train, val y test')
    return scores


def main():
    parser = argparse.ArgumentParser(description='Entrena el autoencoder')
    parser.add_argument('--sin-clickhouse', action='store_true',
                        help='no guardar los scores en anomaly_scores')
    args = parser.parse_args()
    fijar_semillas()

    run_id = f'{MODELO}_{datetime.now():%Y%m%d_%H%M%S}'
    print(f'run_id: {run_id}')
    client = None if args.sin_clickhouse else conectar_clickhouse()
    carpeta_run = crear_carpeta_run(run_id)

    X_train, X_test, X_fit, X_val, X_val_completo, y_val, ids = cargar()
    p = PARAMETROS
    modelo, info = entrenar(X_fit, X_val, p, SEMILLA,
                            X_val_completo=X_val_completo, y_val=y_val)
    ap = info['pr_auc_val']
    print(f'PR-AUC en validación: {ap:.4f}')
    media, std = estabilidad(p, X_fit, X_val, X_val_completo, y_val)

    conjuntos = {'train': X_train, 'val': X_val_completo, 'test': X_test}
    errores = {k: error_reconstruccion(modelo, X) for k, X in conjuntos.items()}
    # Referencia = errores del ajuste (como en los clásicos): fija score_norm y score_pct
    error_fit = error_reconstruccion(modelo, X_fit)
    ref, ref_ordenada = referencia(error_fit), np.sort(error_fit)

    print('\nError de reconstrucción (train completo):')
    print(pd.Series(errores['train']).describe().round(3).to_string())

    explicar(modelo, X_test, errores['test'])
    scores = guardar(modelo, p, conjuntos, errores, ref, ref_ordenada,
                     X_train.shape[1], list(X_train.columns))
    raras = scores['test']['score_pct'] >= 0.99
    print(f'Test por encima del percentil 99 del ajuste: {raras.sum():,} de {len(raras):,} '
          f'({raras.mean() * 100:.2f} %)')

    # Resumen aparte: resumen_entrenamiento.json es solo de los clásicos (lo lee la evaluación)
    with open(os.path.join(CARPETA_MODELOS, f'{MODELO}_resumen.json'), 'w', encoding='utf-8') as fh:
        json.dump({
            'mejores_parametros': p,
            'arquitectura': describir(X_train.shape[1], p),
            'activacion': p.get('activacion'),
            'seleccion': SELECCION,
            'pr_auc_val': float(ap),
            'pr_auc_val_media_semillas': media,
            'pr_auc_val_std_semillas': std,
            'entrenamiento': {**info, 'epocas_max': EPOCAS, 'batch': BATCH,
                              'paciencia': PACIENCIA, 'eval_cada': EVAL_CADA},
            'referencia_normalizacion': ref,
            'proporcion_val': VALIDACION,
            'filas_ajuste': len(X_fit),
            'filas_validacion': len(X_val),
            'semilla': SEMILLA,
            'run_id': run_id,
        }, fh, indent=2, ensure_ascii=False)
    if client is not None:
        guardar_en_clickhouse(client, MODELO, scores, ids, run_id, ref_ordenada)

    for f in (f'{MODELO}.pt', f'{MODELO}_referencia.joblib', f'{MODELO}_resumen.json'):
        guardar_artefacto(os.path.join(CARPETA_MODELOS, f), run_id)
    print(f'Histórico de esta ejecución en "{carpeta_run}"')


if __name__ == '__main__':
    main()
