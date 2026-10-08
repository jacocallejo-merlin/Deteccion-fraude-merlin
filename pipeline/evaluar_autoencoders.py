# --nota guarda una etiqueta en el CSV del histórico para identificar esta ejecución.
# Sin ella, las filas solo se distinguen por la fecha y la arquitectura, y no se sabe
# con qué ruido o con qué prueba se corresponde cada una.
# EJEMPLO: python evaluar_autoencoders.py --nota "cuello 8 ruido 0"


import argparse
import os
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'
CARPETA_RESULTADOS = 'resultados'

MODELO = 'autoencoder'
PRESUPUESTO_PCT = 1.0
PORCENTAJES_TOP = [0.5, 1, 2, 5, 10]
N_BOOTSTRAP = 300
SEMILLA = 42
N_VARIABLES_DETALLE = 6

# Paleta validada para daltonismo (Okabe-Ito).
AZUL, NARANJA = '#0072B2', '#D55E00'
TINTA, TENUE = '#1a1a1a', '#8a8a8a'

pd.set_option('display.width', 160)


# --------------------------------------------------------------------------- carga
def columna_score(df):
    for c in ('error', 'score_norm', 'score'):
        if c in df.columns:
            return c
    raise KeyError(f'sin columna de score reconocible (columnas: {list(df.columns)})')


def cargar():
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    ruta_test = os.path.join(CARPETA_MODELOS, f'{MODELO}_scores_test.parquet')
    if not os.path.exists(ruta_test):
        raise SystemExit(f'No existe {ruta_test}. Lanza antes entrenar_autoencoder.py')
    test = pd.read_parquet(ruta_test).reindex(info_test.index)
    if test.isna().all(axis=1).any():
        raise ValueError('Los scores de test no cuadran con info_test')

    for particion in ('val', 'train'):
        r = os.path.join(CARPETA_MODELOS, f'{MODELO}_scores_{particion}.parquet')
        if os.path.exists(r):
            ref = pd.read_parquet(r)
            break
    else:
        raise SystemExit(f'Falta {MODELO}_scores_val.parquet o {MODELO}_scores_train.parquet')

    col = columna_score(test)
    det = {
        'ref': ref[col].to_numpy(dtype=float),
        'test': test[col].to_numpy(dtype=float),
        'y_ref': info_train.reindex(ref.index)['es_fraude'].astype(int).to_numpy(),
        'particion_ref': particion,
    }
    datos = {
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].astype(object).fillna('-').to_numpy(),
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }

    print(f'Referencia "{particion}": {len(ref):,} filas, {det["y_ref"].sum()} fraudes '
          f'({det["y_ref"].mean() * 100:.2f} %), columna de score "{col}"')
    print(f'Test: {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes '
          f'({datos["y_test"].mean() * 100:.2f} %), {datos["dias_test"]:.0f} días')
    if particion == 'train':
        print('  Aviso: se usan los scores de train como referencia porque no hay val.')
        print('  Es menos limpio que una validación aparte, pero vale para fijar el umbral.')
    return det, datos


def descripcion_arquitectura():
    ruta = os.path.join(CARPETA_MODELOS, f'{MODELO}.pt')
    if not os.path.exists(ruta):
        return None, 'desconocida'
    g = torch.load(ruta, weights_only=False)
    texto = ' -> '.join(str(t) for t in [g['n_entrada'], *g['capas'], g['cuello'],
                                         *g['capas'][::-1], g['n_entrada']])
    return g, texto


# ----------------------------------------------------------------------- métricas
def ic_bootstrap(y, s, n, rng):
    valores = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if y[i].any():
            valores.append(average_precision_score(y[i], s[i]))
    return np.percentile(valores, [2.5, 97.5])


def punto_operacion(y, marcadas, dias):
    vp = int((marcadas & (y == 1)).sum())
    n = int(marcadas.sum())
    prec = vp / n if n else 0.0
    rec = vp / max(1, y.sum())
    return {'marcadas_%': marcadas.mean() * 100, 'alertas_dia': n / dias,
            'precision_%': prec * 100, 'recall_%': rec * 100,
            'f1_%': 200 * prec * rec / (prec + rec) if prec + rec else 0.0}


def tabla_top_k(y, s):
    orden = np.argsort(-s)
    filas = []
    for pct in PORCENTAJES_TOP:
        n = max(1, int(round(len(s) * pct / 100)))
        vp = int(y[orden[:n]].sum())
        filas.append({'top_%': pct, 'revisadas': n, 'fraudes': vp,
                      'precision_%': round(vp / n * 100, 1),
                      'recall_%': round(vp / max(1, y.sum()) * 100, 1),
                      'techo_recall_%': round(min(n, y.sum()) / max(1, y.sum()) * 100, 1),
                      'lift': round(vp / n / y.mean(), 1)})
    return pd.DataFrame(filas)


def recall_por_tipo(det, datos):
    y, tipos = datos['y_test'], datos['tipo_test']
    fraude = y == 1
    total = pd.Series(tipos[fraude]).value_counts()
    detectados = pd.Series(tipos[fraude & det['marca']]).value_counts()
    tabla = pd.DataFrame({'n': total})
    tabla['detectados'] = detectados.reindex(total.index).fillna(0).astype(int)
    tabla['recall_%'] = (tabla['detectados'] / tabla['n'] * 100).round(1)
    return tabla.sort_values('n', ascending=False)


# ------------------------------------------------- análisis propio del autoencoder
def error_por_variable(guardado, X_test):
    from entrenar_autoencoder import Autoencoder

    modelo = Autoencoder(guardado['n_entrada'], guardado['capas'], guardado['cuello'])
    modelo.load_state_dict(guardado['estado'])
    modelo.eval()
    with torch.no_grad():
        datos = torch.tensor(X_test.to_numpy(), dtype=torch.float32)
        return pd.DataFrame(((modelo(datos) - datos) ** 2).numpy(),
                            columns=X_test.columns, index=X_test.index)


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
    tipos = datos['tipo_test']
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
def grafico_errores(det, datos, arquitectura):
    y = datos['y_test']
    s = np.clip(det['test'], 1e-6, None)
    bins = np.logspace(np.log10(s.min()), np.log10(s.max()), 60)
    umbral = det['umbral']

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
    axes[0].set_title(f'Error de reconstrucción en test\n{arquitectura}', fontsize=11)
    axes[0].text(umbral, axes[0].get_ylim()[1] * .98,
                 f'  umbral {PRESUPUESTO_PCT:g} %', va='top', color=TINTA, fontsize=9)
    axes[1].set(xscale='log', xlabel='error de reconstrucción (escala log)')

    ruta = os.path.join(CARPETA_RESULTADOS, 'errores_autoencoder.png')
    fig.tight_layout(); fig.savefig(ruta, dpi=130); plt.close(fig)
    return ruta


def grafico_pr(det, datos, arquitectura):
    y = datos['y_test']
    p, r, _ = precision_recall_curve(y, det['test'])
    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(r, p, lw=2, color=AZUL,
            label=f'autoencoder — PR-AUC {average_precision_score(y, det["test"]):.3f}')
    ax.axhline(y.mean(), ls='--', lw=1.5, color=TENUE, label=f'azar — {y.mean() * 100:.2f} %')
    ax.set(xlabel='Recall', ylabel='Precisión', xlim=(0, 1), ylim=(0, 1.02),
           title=f'Curva precisión-recall en test\n{arquitectura}')
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
    df.to_csv(ruta, mode='a', header=not os.path.exists(ruta), index=False)
    return ruta


# ---------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--nota', default='', help='texto libre para identificar la ejecución')
    parser.add_argument('--bootstrap', type=int, default=N_BOOTSTRAP, help='0 para desactivar')
    args = parser.parse_args()
    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    rng = np.random.default_rng(SEMILLA)

    det, datos = cargar()
    guardado, arquitectura = descripcion_arquitectura()
    print(f'Arquitectura: {arquitectura}')

    y = datos['y_test']
    det['umbral'] = np.quantile(det['ref'], 1 - PRESUPUESTO_PCT / 100)
    det['marca'] = det['test'] >= det['umbral']

    pr = average_precision_score(y, det['test'])
    resumen = {f'PR-AUC_{det["particion_ref"]}': average_precision_score(det['y_ref'], det['ref']),
               'PR-AUC': pr, 'ROC-AUC': roc_auc_score(y, det['test']), 'lift': pr / y.mean()}
    resumen['IC95_inf'], resumen['IC95_sup'] = (
        ic_bootstrap(y, det['test'], args.bootstrap, rng) if args.bootstrap else (np.nan, np.nan))
    operacion = punto_operacion(y, det['marca'], datos['dias_test'])

    print('\n' + '=' * 90)
    print('1. CALIDAD DEL RANKING EN TEST')
    print('=' * 90)
    print(pd.Series(resumen).dropna().round(3).to_string())
    print(f'  Base rate test: {y.mean():.4f} (PR-AUC de un score aleatorio).')

    if args.bootstrap:
        print('  Si los IC95 de dos configuraciones se solapan, la diferencia no es concluyente.')

    print('\n' + '=' * 90)
    print(f'2. PUNTO DE OPERACIÓN   (umbral al {PRESUPUESTO_PCT:g} % fijado en la referencia)')
    print('=' * 90)
    print(pd.Series(operacion).round(1).to_string())

    print('\n' + '=' * 90)
    print('3. SI SE REVISARA SOLO EL TOP K % DEL TEST')
    print('=' * 90)
    print(tabla_top_k(y, det['test']).to_string(index=False))

    print('\n' + '=' * 90)
    print(f'4. RECALL POR TIPO DE FRAUDE   (presupuesto {PRESUPUESTO_PCT:g} %)')
    print('=' * 90)
    print(recall_por_tipo(det, datos).to_string())

    print('\n' + '=' * 90)
    print('5. EXPLICABILIDAD — DESGLOSE DEL ERROR POR VARIABLE')
    print('=' * 90)
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    analisis_variables(guardado, datos, X_test)

    rutas = [grafico_errores(det, datos, arquitectura), grafico_pr(det, datos, arquitectura)]
    historico = registrar({
        'fecha': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'arquitectura': arquitectura,
        'cuello': guardado['cuello'] if guardado else None,
        'nota': args.nota,
        **{k: round(v, 4) for k, v in resumen.items()},
        **{k: round(v, 2) for k, v in operacion.items()},
    })

    print(f'\nFiguras en "{CARPETA_RESULTADOS}/": ' +
          ', '.join(os.path.basename(r) for r in rutas) + '  (se pisan en cada ejecución)')
    print(f'Resultado añadido a {historico}.')


if __name__ == '__main__':
    main()