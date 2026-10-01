import argparse
import itertools
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'
CARPETA_RESULTADOS = 'resultados'

MODELOS = ['iforest', 'kmeans', 'ocsvm']
AGREGACIONES = {'media': np.mean, 'max': np.max}
PRESUPUESTO_PCT = 1.0         
PORCENTAJES_TOP = [0.5, 1, 2, 5, 10]
N_BOOTSTRAP = 300
SEMILLA = 42

pd.set_option('display.width', 160)


def cargar():
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    detectores = {}
    for m in MODELOS:
        rutas = {p: os.path.join(CARPETA_MODELOS, f'{m}_scores_{p}.parquet') for p in ('val', 'test')}
        if not all(os.path.exists(r) for r in rutas.values()):
            print(f'  {m}: sin scores, se omite (python entrenar_modelos.py {m})')
            continue
        val = pd.read_parquet(rutas['val'])
        test = pd.read_parquet(rutas['test']).reindex(info_test.index)
        if test['score_norm'].isna().any():
            raise ValueError(f'{m}: los scores de test no cuadran con info_test')
        detectores[m] = {'val': val['score_norm'].to_numpy(), 'test': test['score_norm'].to_numpy()}
        if 'cluster' in test.columns:
            detectores[m]['cluster_test'] = test['cluster'].to_numpy()
        idx_val = val.index
    if not detectores:
        raise SystemExit('No hay ningún modelo entrenado en modelos/')

    info_val = info_train.loc[idx_val]
    datos = {
        'y_val': info_val['es_fraude'].astype(int).to_numpy(),
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].to_numpy(),
        'ids_test': info_test['transaccion_id'].to_numpy(),
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }
    print(f'Validación: {len(datos["y_val"]):,} filas, {datos["y_val"].sum()} fraudes')
    print(f'Test:       {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes '
          f'({datos["y_test"].mean() * 100:.2f} %), {datos["dias_test"]:.0f} días')
    return detectores, datos


def anadir_combinaciones(detectores):
    simples = list(detectores)
    for r in range(2, len(simples) + 1):
        for combo in itertools.combinations(simples, r):
            for nombre_agg, f in AGREGACIONES.items():
                nombre = f'{nombre_agg}({"+".join(combo)})'
                detectores[nombre] = {
                    p: f(np.column_stack([detectores[m][p] for m in combo]), axis=1)
                    for p in ('val', 'test')}
    return detectores


def ic_bootstrap(y, s, n, rng):
    valores = []
    for _ in range(n):
        i = rng.integers(0, len(y), len(y))
        if y[i].any():
            valores.append(average_precision_score(y[i], s[i]))
    return np.percentile(valores, [2.5, 97.5])


def umbral_presupuesto(s_val, pct):
    return np.quantile(s_val, 1 - pct / 100)


def umbral_f1(y_val, s_val):
    p, r, u = precision_recall_curve(y_val, s_val)
    f1 = 2 * p[:-1] * r[:-1] / np.clip(p[:-1] + r[:-1], 1e-12, None)
    return u[np.argmax(f1)]


def punto_operacion(y, marcadas, dias):
    vp = int((marcadas & (y == 1)).sum())
    n = int(marcadas.sum())
    prec = vp / n if n else 0.0
    rec = vp / max(1, y.sum())
    return {'marcadas_%': marcadas.mean() * 100, 'alertas_dia': n / dias,
            'precision_%': prec * 100, 'recall_%': rec * 100,
            'f1_%': 200 * prec * rec / (prec + rec) if prec + rec else 0.0}


def evaluar_detector(nombre, det, datos, n_boot, rng):
    y_val, y = datos['y_val'], datos['y_test']
    s_val, s = det['val'], det['test']

    det['marca_presupuesto'] = s >= umbral_presupuesto(s_val, PRESUPUESTO_PCT)
    det['marca_f1'] = s >= umbral_f1(y_val, s_val)

    pr = average_precision_score(y, s)
    fila = {'detector': nombre,
            'PR-AUC_val': average_precision_score(y_val, s_val),
            'PR-AUC': pr, 'ROC-AUC': roc_auc_score(y, s), 'lift': pr / y.mean()}
    if n_boot and '(' not in nombre:   
        fila['IC95_inf'], fila['IC95_sup'] = ic_bootstrap(y, s, n_boot, rng)
    for clave, marcas in (('pres', det['marca_presupuesto']), ('f1', det['marca_f1'])):
        for k, v in punto_operacion(y, marcas, datos['dias_test']).items():
            fila[f'{clave}_{k}'] = v
    return fila


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


def recall_por_tipo(marcas, datos):
    y, tipos = datos['y_test'], datos['tipo_test']
    fraude = y == 1
    total = pd.Series(tipos[fraude]).value_counts()
    tabla = pd.DataFrame({'n': total})
    for nombre, m in marcas.items():
        detectados = pd.Series(tipos[fraude & m]).value_counts()
        tabla[nombre] = (detectados.reindex(total.index).fillna(0) / total * 100).round(1)
    return tabla.sort_values('n', ascending=False)


def marcas_reglas(datos):
    import clickhouse_connect
    from BD_VACIAS import DATABASE, HOST, PASSWORD, PORT, USER
    cliente = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                            password=PASSWORD, database=DATABASE)
    ids = {r[0] for r in cliente.query(
        "SELECT DISTINCT transaccion_id FROM alerta WHERE origen = 'regla'").result_rows}
    return np.isin(datos['ids_test'], list(ids))


def grafico_pr(detectores, a_dibujar, datos, reglas):
    y = datos['y_test']
    fig, ax = plt.subplots(figsize=(8, 6))
    for nombre in a_dibujar:
        p, r, _ = precision_recall_curve(y, detectores[nombre]['test'])
        ax.plot(r, p, label=f'{nombre} (PR-AUC {average_precision_score(y, detectores[nombre]["test"]):.3f})',
                lw=2.2 if '(' in nombre else 1.4)
    if reglas is not None:
        op = punto_operacion(y, reglas, datos['dias_test'])
        ax.scatter(op['recall_%'] / 100, op['precision_%'] / 100, marker='*', s=250,
                   c='black', zorder=5, label='reglas')
    ax.axhline(y.mean(), ls='--', c='grey', lw=1, label=f'azar ({y.mean() * 100:.2f} %)')
    ax.set(xlabel='Recall', ylabel='Precisión', title='Curvas precisión-recall en test',
           xlim=(0, 1), ylim=(0, 1.02))
    ax.legend(fontsize=8)
    ax.grid(alpha=.3)
    ruta = os.path.join(CARPETA_RESULTADOS, 'curvas_pr_test.png')
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)
    return ruta


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--reglas', action='store_true', help='comparar con las alertas por reglas')
    parser.add_argument('--bootstrap', type=int, default=N_BOOTSTRAP, help='0 para desactivar')
    args = parser.parse_args()
    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    rng = np.random.default_rng(SEMILLA)

    detectores, datos = cargar()
    simples = list(detectores)
    detectores = anadir_combinaciones(detectores)

    filas = [evaluar_detector(n, d, datos, args.bootstrap, rng) for n, d in detectores.items()]
    tabla = pd.DataFrame(filas).sort_values('PR-AUC_val', ascending=False).reset_index(drop=True)
    elegido = tabla.loc[0, 'detector']    
    tabla.insert(1, 'elegido', np.where(tabla['detector'] == elegido, '<--', ''))
    tabla.round(4).to_csv(os.path.join(CARPETA_RESULTADOS, 'comparativa_test.csv'), index=False)

    print('\n' + '=' * 100)
    print('1. CALIDAD DEL RANKING EN TEST   (ordenado por PR-AUC de validación)')
    print('=' * 100)
    cols = ['detector', 'elegido', 'PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC', 'lift']
    print(tabla[[c for c in cols if c in tabla]].round(3).to_string(index=False))
    print(f'  Base rate test: {datos["y_test"].mean():.4f} (PR-AUC de un score aleatorio).')
    print(f'  El detector elegido es "{elegido}": mejor PR-AUC en validación.')
    print('  Si otro gana en test pero no en validación, elegirlo sería hacer trampa.')

    print('\n' + '=' * 100)
    print('2. PUNTOS DE OPERACIÓN EN TEST   (umbrales fijados en validación)')
    print('=' * 100)
    for clave, titulo in (('pres', f'Presupuesto de revisión: {PRESUPUESTO_PCT:g} % de transacciones'),
                          ('f1', 'Umbral de F1 máximo en validación')):
        print(f'\n{titulo}')
        sub = tabla[['detector'] + [c for c in tabla if c.startswith(clave + '_')]].copy()
        sub.columns = [c[len(clave) + 1:] if c.startswith(clave + '_') else c for c in sub.columns]
        print(sub.round(1).to_string(index=False))
    print('\n  marcadas_% muy distinto del presupuesto = el test se distribuye distinto (deriva).')

    reglas = None
    marcas = {n: detectores[n]['marca_presupuesto'] for n in simples}
    if elegido not in simples:
        marcas[f'{elegido} *'] = detectores[elegido]['marca_presupuesto']
    if args.reglas:
        try:
            reglas = marcas_reglas(datos)
        except Exception as e:
            print(f'\nNo se pudieron leer las reglas de ClickHouse: {e}')
    if reglas is not None:
        union = reglas | detectores[elegido]['marca_presupuesto']
        print('\n' + '=' * 100)
        print('3. REGLAS FRENTE A MODELO EN TEST')
        print('=' * 100)
        comp = pd.DataFrame({
            'reglas': punto_operacion(datos['y_test'], reglas, datos['dias_test']),
            f'{elegido} (presupuesto)': punto_operacion(datos['y_test'],
                                                        detectores[elegido]['marca_presupuesto'],
                                                        datos['dias_test']),
            f'reglas ∪ {elegido}': punto_operacion(datos['y_test'], union, datos['dias_test']),
        }).T
        print(comp.round(1).to_string())
        marcas['reglas'] = reglas
        marcas['reglas ∪ elegido'] = union

    print('\n' + '=' * 100)
    print(f'{4 if reglas is not None else 3}. RECALL % POR TIPO DE FRAUDE '
          f'(presupuesto {PRESUPUESTO_PCT:g} %; elegido: {elegido})')
    print('=' * 100)
    por_tipo = recall_por_tipo(marcas, datos)
    print(por_tipo.to_string())
    por_tipo.to_csv(os.path.join(CARPETA_RESULTADOS, 'recall_por_tipo_test.csv'))
    print('  Un tipo con recall bajo en todos los modelos necesita reglas o features nuevas.')

    print('\n' + '=' * 100)
    print(f'DETALLE DEL ELEGIDO: {elegido} - si se revisara solo el top k % del test')
    print('=' * 100)
    print(tabla_top_k(datos['y_test'], detectores[elegido]['test']).to_string(index=False))

    if 'kmeans' in simples and 'cluster_test' in detectores['kmeans']:
        cl = pd.DataFrame({'cluster': detectores['kmeans']['cluster_test'], 'fraude': datos['y_test']})
        print('\nKMeans - tasa de fraude por cluster (test):')
        print(cl.groupby('cluster')['fraude'].agg(n='size', fraudes='sum', tasa_pct='mean')
              .assign(tasa_pct=lambda t: (t['tasa_pct'] * 100).round(2)).to_string())

    ruta = grafico_pr(detectores, simples + ([elegido] if elegido not in simples else []),
                      datos, reglas)
    print(f'\nGuardado en "{CARPETA_RESULTADOS}/": comparativa_test.csv, '
          f'recall_por_tipo_test.csv y {os.path.basename(ruta)}')


if __name__ == '__main__':
    main()