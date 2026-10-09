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
# Épocas seguidas sin mejorar la pérdida de validación antes de parar.
PACIENCIA = _cfg['paciencia']
# Igual que los clásicos: la validación tiene que ser exactamente las mismas filas.
VALIDACION = CONFIG['modelos']['proporcion_val']
SEMILLA = CONFIG['semilla']
# Se reentrena con estas semillas para ver si el resultado depende del azar.
SEMILLAS_ESTABILIDAD = _cfg['semillas_estabilidad']
# Configuración que se entrena. Para probar otras: pipeline/buscar_autoencoder.py
PARAMETROS = {'capas': list(_cfg['capas']), 'cuello': _cfg['cuello'],
              'ruido': _cfg['ruido'], 'lr': _cfg['lr']}


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
    def __init__(self, n_entrada, capas, cuello):
        super().__init__()
        tamanos = [n_entrada] + list(capas) + [cuello]
        # Cada capa tiene que comprimir: una capa igual o más ancha que la anterior
        # (p. ej. capas [24, 16, 8] con cuello 8 -> 8 -> 8) no aporta nada y solo añade parámetros.
        if any(b >= a for a, b in zip(tamanos[:-1], tamanos[1:])):
            raise ValueError(f'Arquitectura no válida {tamanos}: cada capa del codificador '
                             f'tiene que ser más estrecha que la anterior')

        codificador = []
        for a, b in zip(tamanos[:-1], tamanos[1:]):
            codificador += [nn.Linear(a, b), nn.Tanh()]
        self.codificador = nn.Sequential(*codificador)

        invertido = tamanos[::-1]
        decodificador = []
        for i, (a, b) in enumerate(zip(invertido[:-1], invertido[1:])):
            decodificador.append(nn.Linear(a, b))
            if i < len(invertido) - 2:
                decodificador.append(nn.Tanh())
        self.decodificador = nn.Sequential(*decodificador)

    def forward(self, x):
        return self.decodificador(self.codificador(x))


def describir(n_entrada, p):
    return ' -> '.join(str(t) for t in
                       [n_entrada, *p['capas'], p['cuello'], *p['capas'][::-1], n_entrada])


def entrenar(X_fit, X_val, p, semilla, detalle=True):
    """Entrena con los parámetros p (capas, cuello, ruido, lr).
    Devuelve el modelo con la mejor pérdida de validación y un resumen del entrenamiento."""
    torch.manual_seed(semilla)
    np.random.seed(semilla)

    datos = torch.tensor(X_fit.to_numpy(), dtype=torch.float32)
    validacion = torch.tensor(X_val.to_numpy(), dtype=torch.float32)
    ruido = p['ruido']

    modelo = Autoencoder(datos.shape[1], p['capas'], p['cuello'])
    if detalle:
        parametros = sum(t.numel() for t in modelo.parameters())
        print(f'\nArquitectura: {describir(datos.shape[1], p)}   ({parametros:,} parámetros)')
        print(f'Ruido gaussiano en la entrada: sigma = {ruido}   lr = {p["lr"]}')
        print(f'\n{"época":>6}{"pérdida ajuste":>17}{"pérdida validación":>21}')

    optimizador = torch.optim.Adam(modelo.parameters(), lr=p['lr'])
    criterio = nn.MSELoss()
    generador = torch.Generator().manual_seed(semilla)

    mejor, mejor_estado, mejor_epoca, sin_mejorar = float('inf'), None, 0, 0
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

        if detalle and (epoca % 50 == 0 or epoca == 1):
            print(f'{epoca:>6}{perdida_ajuste:>17.5f}{perdida_val:>21.5f}')

        if perdida_val < mejor - 1e-5:
            mejor, mejor_epoca, sin_mejorar = perdida_val, epoca, 0
            mejor_estado = {k: v.clone() for k, v in modelo.state_dict().items()}
        else:
            sin_mejorar += 1
            if sin_mejorar >= PACIENCIA:
                break

    if mejor_estado is None:
        raise RuntimeError('La pérdida de validación nunca fue finita (¿NaN en los datos?)')
    modelo.load_state_dict(mejor_estado)
    if detalle:
        print(f'{epoca:>6}   parada temprana. Mejor pérdida de validación: {mejor:.5f} '
              f'(época {mejor_epoca})')
    return modelo, {'perdida_val': mejor, 'mejor_epoca': mejor_epoca, 'epocas': epoca}


def error_reconstruccion(modelo, X):
    modelo.eval()
    with torch.no_grad():
        datos = torch.tensor(X.to_numpy(), dtype=torch.float32)
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
        modelo, _ = entrenar(X_fit, X_val, p, semilla, detalle=False)
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
                'ruido': p['ruido'], 'lr': p['lr'], 'n_entrada': n_entrada, 'columnas': columnas},
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
    modelo, info = entrenar(X_fit, X_val, p, SEMILLA)
    ap = average_precision_score(y_val, error_reconstruccion(modelo, X_val_completo))
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
            'pr_auc_val': float(ap),
            'pr_auc_val_media_semillas': media,
            'pr_auc_val_std_semillas': std,
            'entrenamiento': {**info, 'epocas_max': EPOCAS, 'batch': BATCH, 'paciencia': PACIENCIA},
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
