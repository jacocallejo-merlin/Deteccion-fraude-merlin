"""
Análisis detallado del autoencoder.

Las métricas (PR-AUC, IC95 con bootstrap por cliente, umbrales, top k %, recall por tipo)
son las MISMAS funciones que usa evaluar_modelos_nosupervisados.py, así los números son
comparables con los clásicos. Aquí solo se añade lo propio del autoencoder: el error por
variable, el histograma de errores y el histórico de configuraciones.

Cada ejecución se apunta en resultados/historico_autoencoders.csv con una etiqueta automática
(run_id, arquitectura, ruido, lr, épocas). --nota añade un texto libre opcional.
EJEMPLO: python pipeline/evaluar_autoencoders.py --nota "prueba con más datos"
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import average_precision_score, precision_recall_curve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_MODELOS, CARPETA_RESULTADOS
from evaluar_modelos_nosupervisados import (COLUMNA_GRUPO, N_BOOTSTRAP, PRESUPUESTO_PCT,
                                            TOLERANCIA_DERIVA, ap_bootstrap, evaluar_detector,
                                            recall_por_tipo, tabla_top_k)

MODELO = 'autoencoder'
N_VARIABLES_DETALLE = 6

AZUL, NARANJA = '#0072B2', '#D55E00'
TINTA, TENUE = '#1a1a1a', '#8a8a8a'

pd.set_option('display.width', 160)


# --------------------------------------------------------------------------- carga
def cargar():
    """Mismo formato de 'datos' que evaluar_modelos_nosupervisados.cargar()."""
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    rutas = {p: os.path.join(CARPETA_MODELOS, f'{MODELO}_scores_{p}.parquet') for p in ('val', 'test')}
    if not all(os.path.exists(r) for r in rutas.values()):
        raise SystemExit(f'Faltan los scores de val/test del {MODELO}. '
                         'Lanza antes pipeline/entrenar_autoencoder.py')
    val = pd.read_parquet(rutas['val'])
    test = pd.read_parquet(rutas['test']).reindex(info_test.index)
    if test['score'].isna().any():
        raise ValueError('Los scores de test no cuadran con info_test')

    info_val = info_train.loc[val.index]
    grupo = COLUMNA_GRUPO if (COLUMNA_GRUPO in info_train and COLUMNA_GRUPO in info_test) else None
    # 'score' = error de reconstrucción (mismo orden que score_norm, pero en escala natural)
    det = {'val': val['score'].to_numpy(dtype=float), 'test': test['score'].to_numpy(dtype=float)}
    datos = {
        'y_val': info_val['es_fraude'].astype(int).to_numpy(),
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].to_numpy(),
        'grupo_val': info_val[grupo].to_numpy() if grupo else None,
        'grupo_test': info_test[grupo].to_numpy() if grupo else None,
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }
    print(f'Validación: {len(datos["y_val"]):,} filas, {datos["y_val"].sum()} fraudes')
    print(f'Test:       {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes '
          f'({datos["y_test"].mean() * 100:.2f} %), {datos["dias_test"]:.0f} días')
    print(f'Bootstrap:  por {"cliente (" + grupo + ")" if grupo else "transacción"}')
    return det, datos


def cargar_guardado():
    """Pesos del .pt y resumen del entrenamiento (para la etiqueta automática)."""
    ruta_pt = os.path.join(CARPETA_MODELOS, f'{MODELO}.pt')
    guardado = torch.load(ruta_pt, weights_only=False) if os.path.exists(ruta_pt) else None
    ruta_resumen = os.path.join(CARPETA_MODELOS, f'{MODELO}_resumen.json')
    resumen = {}
    if os.path.exists(ruta_resumen):
        with open(ruta_resumen, encoding='utf-8') as fh:
            resumen = json.load(fh)
    return guardado, resumen


def arquitectura(guardado):
    if guardado is None:
        return 'desconocida'
    return ' -> '.join(str(t) for t in [guardado['n_entrada'], *guardado['capas'], guardado['cuello'],
                                        *guardado['capas'][::-1], guardado['n_entrada']])


# ------------------------------------------------- análisis propio del autoencoder
def error_por_variable(guardado, X_test):
    from entrenar_autoencoder import Autoencoder, error_por_variable as detalle

    modelo = Autoencoder(guardado['n_entrada'], guardado['capas'], guardado['cuello'])
    modelo.load_state_dict(guardado['estado'])
    return detalle(modelo, X_test[guardado['columnas']])


def analisis_variables(guardado, datos, X_test):
    if guardado is None:
        print(f'\nNo está {MODELO}.pt, se omite el desglose por variable.')
        return
    detalle = error_por_variable(guardado, X_test)
    y = datos['y_test']

    print('\nQué variables fallan en cada grupo (error medio por variable, test):')
    comp = pd.DataFrame({'legítimas': detalle[y == 0].mean().astype(float),
                         'fraude': detalle[y == 1].mean().astype(float)})
    comp['veces'] = (comp['fraude'] / comp['legítimas'].clip(lower=1e-9)).round(0).astype(int)
    print(comp.sort_values('veces', ascending=False).head(N_VARIABLES_DETALLE).round(3).to_string())

    print('\nVariable que más error aporta a cada tipo de fraude:')
    tipos = pd.Series(datos['tipo_test']).fillna('-').to_numpy()
    filas = []
    for tipo in pd.Series(tipos[y == 1]).value_counts().index:
        sel = tipos == tipo
        medio = detalle[sel].mean().sort_values(ascending=False)
        filas.append({'tipo': tipo, 'n': int(sel.sum()),
                      'error_medio': round(float(detalle[sel].sum(axis=1).mean()), 2),
                      'principal': medio.index[0],
                      'peso_%': round(float(medio.iloc[0] / medio.sum() * 100)),
                      'segunda': medio.index[1]})
    print(pd.DataFrame(filas).to_string(index=False))


# ------------------------------------------------------------------------ gráficos
def grafico_errores(det, datos, umbral, titulo):
    y = datos['y_test']
    s = np.clip(det['test'], 1e-6, None)
    bins = np.logspace(np.log10(s.min()), np.log10(s.max()), 60)

    fig, axes = plt.subplots(2, 1, figsize=(9, 6), sharex=True)
    for ax, mascara, color, etiqueta in ((axes[0], y == 0, AZUL, 'legítimas'),
                                         (axes[1], y == 1, NARANJA, 'fraude')):
        ax.hist(s[mascara], bins=bins, density=True, color=color)
        ax.axvline(umbral, color=TINTA, ls='--', lw=2)
        ax.set_ylabel('densidad')
        ax.text(.01, .9, f'{etiqueta}  (n = {int(mascara.sum()):,})', transform=ax.transAxes,
                color=color, fontweight='bold')
        ax.grid(alpha=.25, color=TENUE)
        for lado in ('top', 'right'):
            ax.spines[lado].set_visible(False)
    axes[0].set_title(f'Error de reconstrucción en test\n{titulo}', fontsize=11)
    axes[0].text(umbral, axes[0].get_ylim()[1] * .98,
                 f'  umbral {PRESUPUESTO_PCT:g} %', va='top', color=TINTA, fontsize=9)
    axes[1].set(xscale='log', xlabel='error de reconstrucción (escala log)')

    ruta = os.path.join(CARPETA_RESULTADOS, 'errores_autoencoder.png')
    fig.tight_layout(); fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def grafico_pr(det, datos, titulo):
    y = datos['y_test']
    p, r, _ = precision_recall_curve(y, det['test'])
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(r, p, lw=2, color=AZUL,
            label=f'autoencoder — PR-AUC {average_precision_score(y, det["test"]):.3f}')
    ax.axhline(y.mean(), ls='--', lw=1.5, color=TENUE, label=f'azar — {y.mean() * 100:.2f} %')
    ax.set(xlabel='Recall', ylabel='Precisión', xlim=(0, 1), ylim=(0, 1.02),
           title=f'Curva precisión-recall en test\n{titulo}')
    ax.legend(fontsize=9, frameon=False)
    ax.grid(alpha=.25, color=TENUE)
    for lado in ('top', 'right'):
        ax.spines[lado].set_visible(False)

    ruta = os.path.join(CARPETA_RESULTADOS, 'curva_pr_autoencoder.png')
    fig.tight_layout(); fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def registrar(fila):
    ruta = os.path.join(CARPETA_RESULTADOS, 'historico_autoencoders.csv')
    df = pd.DataFrame([fila])
    if os.path.exists(ruta):
        cabecera = pd.read_csv(ruta, nrows=0).columns
        df = df.reindex(columns=list(cabecera) + [c for c in df.columns if c not in cabecera])
        # Si hay columnas nuevas, se reescribe el fichero entero para no descuadrar la cabecera
        if len(df.columns) > len(cabecera):
            pd.concat([pd.read_csv(ruta), df], ignore_index=True).to_csv(ruta, index=False)
            return ruta
    df.to_csv(ruta, mode='a', header=not os.path.exists(ruta), index=False)
    return ruta


# ---------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--nota', default='', help='texto libre opcional para esta ejecución')
    parser.add_argument('--bootstrap', type=int, default=N_BOOTSTRAP, help='0 para desactivar')
    args = parser.parse_args()
    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)

    det, datos = cargar()
    guardado, resumen = cargar_guardado()
    p = resumen.get('mejores_parametros', {})
    entreno = resumen.get('entrenamiento', {})
    titulo = arquitectura(guardado)
    print(f'Arquitectura: {titulo}   ruido {p.get("ruido", "?")}   lr {p.get("lr", "?")}   '
          f'run_id {resumen.get("run_id", "?")}')
    y = datos['y_test']

    # Mismas métricas y umbrales que los clásicos
    fila = evaluar_detector(MODELO, det, datos)
    if args.bootstrap:
        aps = ap_bootstrap(y, [det['test']], args.bootstrap, datos['grupo_test'])[:, 0]
        fila['IC95_inf'], fila['IC95_sup'] = np.percentile(aps, [2.5, 97.5])

    print('\n' + '=' * 90)
    print('1. CALIDAD DEL RANKING')
    print('=' * 90)
    cols = ['PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC', 'lift']
    print(pd.Series({c: fila[c] for c in cols if c in fila}).round(3).to_string())
    print(f'  Base rate test: {y.mean():.4f} (PR-AUC de un score aleatorio).')

    print('\n' + '=' * 90)
    print('2. PUNTOS DE OPERACIÓN EN TEST   (umbrales fijados en validación)')
    print('=' * 90)
    puntos = pd.DataFrame({clave: {k[len(clave) + 1:]: v for k, v in fila.items()
                                   if k.startswith(clave + '_')}
                           for clave in ('pres', 'f1')})
    puntos.columns = [f'presupuesto {PRESUPUESTO_PCT:g} %', 'F1 máximo en val']
    print(puntos.round(1).to_string())
    marcadas = fila['pres_marcadas_%']
    if not PRESUPUESTO_PCT / TOLERANCIA_DERIVA <= marcadas <= PRESUPUESTO_PCT * TOLERANCIA_DERIVA:
        print(f'\n  AVISO de deriva: marca el {marcadas:.2f} % del test, '
              f'lejos del {PRESUPUESTO_PCT:g} % previsto')

    print('\n' + '=' * 90)
    print('3. SI SE REVISARA SOLO EL TOP K % DEL TEST')
    print('=' * 90)
    print(tabla_top_k(y, det['test']).to_string(index=False))

    print('\n' + '=' * 90)
    print(f'4. RECALL % POR TIPO DE FRAUDE   (presupuesto {PRESUPUESTO_PCT:g} %)')
    print('=' * 90)
    print(recall_por_tipo({MODELO: det['marca_presupuesto']}, datos).to_string())

    print('\n' + '=' * 90)
    print('5. EXPLICABILIDAD — DESGLOSE DEL ERROR POR VARIABLE')
    print('=' * 90)
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    analisis_variables(guardado, datos, X_test)

    umbral = np.quantile(det['val'], 1 - PRESUPUESTO_PCT / 100)
    rutas = [grafico_errores(det, datos, umbral, titulo), grafico_pr(det, datos, titulo)]
    historico = registrar({
        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'run_id': resumen.get('run_id'),
        'arquitectura': titulo,
        'cuello': guardado['cuello'] if guardado else None,
        'ruido': p.get('ruido'),
        'lr': p.get('lr'),
        'epocas': entreno.get('epocas'),
        'mejor_epoca': entreno.get('mejor_epoca'),
        'nota': args.nota,
        **{c: round(fila[c], 4) for c in cols if c in fila},
        **{k: round(v, 2) for k, v in fila.items() if k.startswith(('pres_', 'f1_'))},
    })

    print(f'\nFiguras en "{CARPETA_RESULTADOS}/": ' +
          ', '.join(os.path.basename(r) for r in rutas) + '  (se pisan en cada ejecución)')
    print(f'Resultado añadido a {historico}.')


if __name__ == '__main__':
    main()
