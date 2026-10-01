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
COLUMNA_GRUPO = 'cliente_id'   

MODELOS = ['iforest', 'kmeans', 'ocsvm']
AGREGACIONES = {'media': np.mean, 'max': np.max}
PRESUPUESTO_PCT = 1.0
PORCENTAJES_TOP = [0.5, 1, 2, 5, 10]
N_BOOTSTRAP = 300
N_TOP_VALIDACION = 4           
TOLERANCIA_DERIVA = 1.5        
SEMILLA = 42

pd.set_option('display.width', 160)


def cargar(carpeta):
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    detectores, idx_val, primero = {}, None, None
    for m in MODELOS:
        rutas = {p: os.path.join(carpeta, f'{m}_scores_{p}.parquet') for p in ('val', 'test')}
        if not all(os.path.exists(r) for r in rutas.values()):
            print(f'  {m}: sin scores en {carpeta}/, se omite (python modelos_nosupervisados.py {m})')
            continue

        val = pd.read_parquet(rutas['val'])
        if idx_val is None:
            idx_val, primero = val.index, m
        elif not val.index.equals(idx_val):
            raise ValueError(f'{m}: los scores de validación no tienen las mismas filas que los de '
                             f'{primero}. ¿Se reentrenó alguno con otros datos?')

        test = pd.read_parquet(rutas['test']).reindex(info_test.index)
        if test['score_norm'].isna().any():
            raise ValueError(f'{m}: los scores de test no cuadran con info_test')

        detectores[m] = {'val': val['score_norm'].to_numpy(), 'test': test['score_norm'].to_numpy()}
        if 'cluster' in test.columns:
            detectores[m]['cluster_test'] = test['cluster'].to_numpy()
    if not detectores:
        raise SystemExit(f'No hay ningún modelo entrenado en {carpeta}/')

    faltan = idx_val.difference(info_train.index)
    if len(faltan):
        raise ValueError(f'{len(faltan)} filas de validación no están en info_train')
    info_val = info_train.loc[idx_val]

    grupo = COLUMNA_GRUPO if (COLUMNA_GRUPO in info_train and COLUMNA_GRUPO in info_test) else None
    datos = {
        'y_val': info_val['es_fraude'].astype(int).to_numpy(),
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].to_numpy(),
        'ids_test': info_test['transaccion_id'].to_numpy(),
        'grupo_val': info_val[grupo].to_numpy() if grupo else None,
        'grupo_test': info_test[grupo].to_numpy() if grupo else None,
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }
    for parte in ('val', 'test'):
        if datos[f'y_{parte}'].sum() == 0:
            raise ValueError(f'No hay fraudes en {parte}: no se pueden calcular las métricas')

    print(f'Modelos:    {carpeta}/  ({", ".join(detectores)})')
    print(f'Validación: {len(datos["y_val"]):,} filas, {datos["y_val"].sum()} fraudes')
    print(f'Test:       {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes '
          f'({datos["y_test"].mean() * 100:.2f} %), {datos["dias_test"]:.0f} días')
    print(f'Bootstrap:  por {"cliente (" + grupo + ")" if grupo else "transacción"}')
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


def preparar_grupos(grupos):
    codigos, _ = pd.factorize(grupos)
    orden = np.argsort(codigos, kind='stable')
    tam = np.bincount(codigos)
    inicio = np.concatenate([[0], np.cumsum(tam)[:-1]])
    return orden, inicio, tam


def remuestra(rng, n_filas, prep):
    if prep is None:
        return rng.integers(0, n_filas, n_filas)
    orden, inicio, tam = prep
    g = rng.integers(0, len(tam), len(tam))          
    t = tam[g]
    fin = np.cumsum(t)
    pos = np.repeat(inicio[g], t) + np.arange(fin[-1]) - np.repeat(fin - t, t)
    return orden[pos]


def ap_bootstrap(y, lista_scores, n, grupos=None, semilla=SEMILLA):
    rng = np.random.default_rng(semilla)
    prep = preparar_grupos(grupos) if grupos is not None else None
    res = np.empty((n, len(lista_scores)))
    b = 0
    while b < n:
        i = remuestra(rng, len(y), prep)
        if not y[i].any():
            continue
        res[b] = [average_precision_score(y[i], s[i]) for s in lista_scores]
        b += 1
    return res


def bootstrap_eleccion(tabla, detectores, datos, n, n_top=N_TOP_VALIDACION):
    nombres = tabla['detector'].head(n_top).tolist()
    aps = ap_bootstrap(datos['y_val'], [detectores[x]['val'] for x in nombres], n,
                       datos['grupo_val'])
    filas = []
    for j, nombre in enumerate(nombres):
        dif_inf, dif_sup = np.percentile(aps[:, 0] - aps[:, j], [2.5, 97.5])
        if j == 0:
            veredicto = 'elegido'
        elif dif_inf > 0:
            veredicto = 'peor'
        elif dif_sup < 0:
            veredicto = 'mejor (!)'
        else:
            veredicto = 'empate'
        filas.append({'detector': nombre, 'PR-AUC_val': tabla['PR-AUC_val'].iloc[j],
                      'dif_inf': dif_inf, 'dif_sup': dif_sup, 'veredicto': veredicto})
    return pd.DataFrame(filas)


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


def evaluar_detector(nombre, det, datos):
    y_val, y = datos['y_val'], datos['y_test']
    s_val, s = det['val'], det['test']

    det['marca_presupuesto'] = s >= umbral_presupuesto(s_val, PRESUPUESTO_PCT)
    det['marca_f1'] = s >= umbral_f1(y_val, s_val)

    pr = average_precision_score(y, s)
    fila = {'detector': nombre,
            'PR-AUC_val': average_precision_score(y_val, s_val),
            'PR-AUC': pr, 'ROC-AUC': roc_auc_score(y, s), 'lift': pr / y.mean()}
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


def grafico_pr(detectores, a_dibujar, datos, reglas, carpeta):
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
    ruta = os.path.join(carpeta, 'curvas_pr_test.png')
    fig.tight_layout()
    fig.savefig(ruta, dpi=130)
    plt.close(fig)
    return ruta

class Secciones:
    def __init__(self):
        self.n = 0

    def __call__(self, titulo):
        self.n += 1
        print('\n' + '=' * 100)
        print(f'{self.n}. {titulo}')
        print('=' * 100)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--carpeta', default=CARPETA_MODELOS,
                        help='carpeta de modelos (modelos, modelos_sinfraude, modelos_rapido...)')
    parser.add_argument('--reglas', action='store_true', help='comparar con las alertas por reglas')
    parser.add_argument('--bootstrap', type=int, default=N_BOOTSTRAP, help='0 para desactivar')
    args = parser.parse_args()

    salida = os.path.join(CARPETA_RESULTADOS, os.path.basename(os.path.normpath(args.carpeta)))
    os.makedirs(salida, exist_ok=True)
    seccion = Secciones()

    detectores, datos = cargar(args.carpeta)
    simples = list(detectores)
    detectores = anadir_combinaciones(detectores)

    filas = [evaluar_detector(n, d, datos) for n, d in detectores.items()]
    tabla = pd.DataFrame(filas).sort_values('PR-AUC_val', ascending=False).reset_index(drop=True)
    elegido = tabla.loc[0, 'detector']
    principales = simples + ([elegido] if elegido not in simples else [])

    if args.bootstrap:
        aps = ap_bootstrap(datos['y_test'], [detectores[n]['test'] for n in principales],
                           args.bootstrap, datos['grupo_test'])
        inf, sup = np.percentile(aps, [2.5, 97.5], axis=0)
        tabla = tabla.merge(pd.DataFrame({'detector': principales,
                                          'IC95_inf': inf, 'IC95_sup': sup}),
                            on='detector', how='left')
    tabla.insert(1, 'elegido', np.where(tabla['detector'] == elegido, '<--', ''))
    tabla.round(4).to_csv(os.path.join(salida, 'comparativa_test.csv'), index=False)

    seccion('CALIDAD DEL RANKING EN TEST   (ordenado por PR-AUC de validación)')
    cols = ['detector', 'elegido', 'PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC', 'lift']
    print(tabla[[c for c in cols if c in tabla]].round(3).to_string(index=False))
    print(f'  Base rate test: {datos["y_test"].mean():.4f} (PR-AUC de un score aleatorio).')
    print(f'  El detector elegido es "{elegido}": mejor PR-AUC en validación.')
    print('  La PR-AUC_val del elegido es optimista (es la mejor de varias); es normal que baje en test.')

    if args.bootstrap:
        seccion(f'¿EL ELEGIDO GANA DE VERDAD?   (bootstrap pareado en validación, '
                f'dif = PR-AUC elegido - PR-AUC fila)')
        eleccion = bootstrap_eleccion(tabla, detectores, datos, args.bootstrap)
        print(eleccion.round(4).to_string(index=False))
        eleccion.round(4).to_csv(os.path.join(salida, 'bootstrap_eleccion_val.csv'), index=False)
        empatan = eleccion.loc[eleccion['veredicto'] == 'empate', 'detector'].tolist()
        if empatan:
            print(f'  Empatan con el elegido: {empatan}.')
            simples_empate = [e for e in empatan if e in simples]
            if elegido not in simples and simples_empate:
                print(f'  Un modelo simple ({simples_empate[0]}) empata con la combinación elegida: '
                      f'es más barato de mantener y explicar.')
        else:
            print('  El elegido supera de forma clara a los siguientes.')

    seccion('PUNTOS DE OPERACIÓN EN TEST   (umbrales fijados en validación)')
    for clave, titulo in (('pres', f'Presupuesto de revisión: {PRESUPUESTO_PCT:g} % de transacciones'),
                          ('f1', 'Umbral de F1 máximo en validación (ruidoso con pocos fraudes)')):
        print(f'\n{titulo}')
        sub = tabla[['detector'] + [c for c in tabla if c.startswith(clave + '_')]].copy()
        sub.columns = [c[len(clave) + 1:] if c.startswith(clave + '_') else c for c in sub.columns]
        print(sub.round(1).to_string(index=False))
    marcadas = tabla.set_index('detector')['pres_marcadas_%']
    fuera = marcadas[(marcadas > PRESUPUESTO_PCT * TOLERANCIA_DERIVA)
                     | (marcadas < PRESUPUESTO_PCT / TOLERANCIA_DERIVA)]
    if len(fuera):
        print(f'\n  AVISO de deriva: marcan en test lejos del {PRESUPUESTO_PCT:g} % previsto -> '
              + ', '.join(f'{n} ({v:.2f} %)' for n, v in fuera.items()))
        print('  El umbral de validación no se traslada bien a test para esos detectores.')
    else:
        print(f'\n  Todos marcan en test cerca del {PRESUPUESTO_PCT:g} % previsto: '
              f'los umbrales se trasladan bien.')

    reglas = None
    marcas = {n: detectores[n]['marca_presupuesto'] for n in simples}
    if elegido not in simples:
        marcas[f'{elegido} *'] = detectores[elegido]['marca_presupuesto']
    if args.reglas:
        try:
            reglas = marcas_reglas(datos)
        except Exception as e:
            print(f'\nNo se pudieron leer las reglas de ClickHouse ({type(e).__name__}: {e})')

    if reglas is not None:
        y, dias = datos['y_test'], datos['dias_test']
        vol_reglas = reglas.mean() * 100
        marca_igual = detectores[elegido]['test'] >= umbral_presupuesto(
            detectores[elegido]['val'], vol_reglas)
        union = reglas | detectores[elegido]['marca_presupuesto']

        seccion('REGLAS FRENTE A MODELO EN TEST')
        comp = pd.DataFrame({
            'reglas': punto_operacion(y, reglas, dias),
            f'{elegido} (presupuesto {PRESUPUESTO_PCT:g} %)':
                punto_operacion(y, detectores[elegido]['marca_presupuesto'], dias),
            f'{elegido} (mismo volumen que reglas)': punto_operacion(y, marca_igual, dias),
            f'reglas ∪ {elegido}': punto_operacion(y, union, dias),
        }).T
        print(comp.round(1).to_string())
        print(f'  Las reglas marcan el {vol_reglas:.2f} % del test. La fila "mismo volumen" es la '
              f'comparación justa entre reglas y modelo.')
        comp.round(2).to_csv(os.path.join(salida, 'reglas_vs_modelo_test.csv'))
        marcas['reglas'] = reglas
        marcas['reglas ∪ elegido'] = union

    seccion(f'RECALL % POR TIPO DE FRAUDE (presupuesto {PRESUPUESTO_PCT:g} %; elegido: {elegido})')
    por_tipo = recall_por_tipo(marcas, datos)
    print(por_tipo.to_string())
    por_tipo.to_csv(os.path.join(salida, 'recall_por_tipo_test.csv'))

    seccion(f'DETALLE DEL ELEGIDO: {elegido} - si se revisara solo el top k % del test')
    print(tabla_top_k(datos['y_test'], detectores[elegido]['test']).to_string(index=False))

    if 'kmeans' in simples and 'cluster_test' in detectores['kmeans']:
        cl = pd.DataFrame({'cluster': detectores['kmeans']['cluster_test'], 'fraude': datos['y_test']})
        print('\nKMeans - tasa de fraude por cluster (test):')
        print(cl.groupby('cluster')['fraude'].agg(n='size', fraudes='sum', tasa_pct='mean')
              .assign(tasa_pct=lambda t: (t['tasa_pct'] * 100).round(2)).to_string())

    ruta = grafico_pr(detectores, principales, datos, reglas, salida)
    print(f'\nResultados guardados en "{salida}/" (CSV y {os.path.basename(ruta)})')


if __name__ == '__main__':
    main()