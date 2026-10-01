import argparse
import itertools
import json
import os
import time

import joblib
import numpy as np
import pandas as pd
from joblib import Parallel, delayed
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score, pairwise_distances, roc_auc_score
from sklearn.svm import OneClassSVM

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

SEMILLA = 42
FRAC_VALIDACION = 0.2
TOP_PCT = 1.0
N_JOBS = -1
SEMILLAS_ESTABILIDAD = [0, 7, 123]

N_BOOTSTRAP = 200             
N_TOP_BOOTSTRAP = 3             
NIVELES_PERCENTIL = np.linspace(0, 1, 1001) 

KMEANS_N_INIT = 5
OCSVM_N_MUESTRA = 10_000
OCSVM_BLOQUE = 20_000

REJILLAS = {
    'iforest': {
        'n_estimators': [100, 300],
        'max_samples': [256, 1024, 4096],
        'max_features': [0.5, 0.75, 1.0],
    },
    'kmeans': {
        'k': [2, 3, 4, 5, 6, 8, 10, 12],
    },
    'ocsvm': {
        'gamma_factor': [0.25, 0.5, 1, 2, 4],
        'nu': [0.005, 0.01, 0.05],
    },
}

MODELOS = list(REJILLAS)
COL_TOP = f'recall_top{TOP_PCT:g}%'


def cargar(sin_fraudes=False):
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    ts_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'),
                              columns=['timestamp'])['timestamp']

    if not X_train.index.equals(info_train.index):
        raise ValueError('X_train e info_train no tienen el mismo índice')
    if not info_train['timestamp'].is_monotonic_increasing:
        raise ValueError('info_train no está ordenado por timestamp: el corte '
                         'entreno/validación no sería temporal')
    if info_train['timestamp'].iloc[-1] > ts_test.min():
        raise ValueError(f'Hay transacciones de test ({ts_test.min()}) anteriores al final '
                         f'del train ({info_train["timestamp"].iloc[-1]})')

    corte = int(len(X_train) * (1 - FRAC_VALIDACION))
    y_train = info_train['es_fraude'].astype(int).to_numpy()
    y_entreno, y_val = y_train[:corte], y_train[corte:]
    if y_val.sum() == 0:
        raise ValueError('No hay fraudes en validación: no se puede elegir el modelo')

    d = {
        'columnas': list(X_train.columns),
        'idx_train': X_train.index, 'idx_test': X_test.index,
        'idx_entreno': X_train.index[:corte], 'idx_val': X_train.index[corte:],
        'train': np.ascontiguousarray(X_train.to_numpy(np.float64)),
        'test': np.ascontiguousarray(X_test.to_numpy(np.float64)),
        'y_val': y_val,
    }
    d['entreno'], d['val'] = d['train'][:corte], d['train'][corte:]

    if sin_fraudes:
        d['ajuste_entreno'] = d['entreno'][y_entreno == 0]
        d['ajuste_train'] = d['train'][y_train == 0]
    else:
        d['ajuste_entreno'], d['ajuste_train'] = d['entreno'], d['train']

    print(f'Variables: {len(d["columnas"])}')
    print(f'Entreno:    {len(d["entreno"]):>9,} filas   (hasta {info_train["timestamp"].iloc[corte - 1]})')
    print(f'Validación: {len(d["val"]):>9,} filas   ({y_val.sum()} fraudes, {y_val.mean() * 100:.2f} %)')
    print(f'Test:       {len(d["test"]):>9,} filas   (no se usa aquí para decidir nada)')
    if sin_fraudes:
        print(f'Ajuste SIN fraudes confirmados: se quitan {y_entreno.sum()} de entreno '
              f'y {y_train.sum()} de train')
    if y_val.sum() < 20:
        print('  AVISO: pocos fraudes en validación, la elección de hiperparámetros será ruidosa')
    return d

def metricas_top(y, s, pct=TOP_PCT):
    n = max(1, int(round(len(s) * pct / 100)))
    vp = y[np.argpartition(-s, n - 1)[:n]].sum()
    prec, rec = vp / n, vp / max(1, y.sum())
    return rec, prec, 2 * prec * rec / max(1e-12, prec + rec)


def metricas(y, s):
    rec, prec, f1 = metricas_top(y, s)
    return {'pr_auc': average_precision_score(y, s),
            'roc_auc': roc_auc_score(y, s),
            COL_TOP: rec,
            'precision_top': prec,
            'f1_top': f1}


def referencia(s):
    return {'cuantiles': np.quantile(s, NIVELES_PERCENTIL),
            'mediana': float(np.median(s)),
            'p99': float(np.quantile(s, 0.99))}


def percentil(cuantiles, s):
 
    return np.interp(s, cuantiles, NIVELES_PERCENTIL)


def normalizar(s, mediana, p99):
    return (s - mediana) / max(p99 - mediana, 1e-12)

def gamma_mediana(X, semilla=SEMILLA):
    rng = np.random.default_rng(semilla)
    m = X[rng.choice(len(X), min(2000, len(X)), replace=False)]
    d2 = pairwise_distances(m, metric='sqeuclidean')
    return 1.0 / np.median(d2[np.triu_indices_from(d2, k=1)])


def ajustar(nombre, p, X, semilla=SEMILLA, n_jobs=N_JOBS):
    art = {'tipo': nombre, 'params': dict(p)}

    if nombre == 'iforest':
        art['modelo'] = IsolationForest(
            n_estimators=p['n_estimators'], max_samples=min(p['max_samples'], len(X)),
            max_features=p['max_features'], random_state=semilla, n_jobs=n_jobs).fit(X)

    elif nombre == 'kmeans':
        m = KMeans(n_clusters=p['k'], n_init=KMEANS_N_INIT, random_state=semilla).fit(X)
        dmin = m.transform(X)[np.arange(len(X)), m.labels_]
        escala = np.array([np.median(dmin[m.labels_ == c]) for c in range(p['k'])])
        art['modelo'], art['escala'] = m, np.maximum(escala, 1e-9)

    elif nombre == 'ocsvm':
        rng = np.random.default_rng(semilla)
        filas = rng.choice(len(X), min(OCSVM_N_MUESTRA, len(X)), replace=False)
        art['modelo'] = OneClassSVM(kernel='rbf', nu=p['nu'], gamma=p['gamma'],
                                    cache_size=1000).fit(X[filas])
    else:
        raise ValueError(nombre)
    return art


def puntuar(art, X, n_jobs=1):
    m, tipo = art['modelo'], art['tipo']

    if tipo == 'iforest':
        return -m.score_samples(X)

    if tipo == 'kmeans':
        d = m.transform(X)
        c = d.argmin(axis=1)
        dmin = d[np.arange(len(X)), c]
        return dmin / art['escala'][c] if art['params']['distancia'] == 'relativa' else dmin

    if tipo == 'ocsvm':
        if n_jobs == 1 or len(X) <= OCSVM_BLOQUE:
            return -m.decision_function(X)
        bloques = Parallel(n_jobs=n_jobs)(
            delayed(m.decision_function)(X[i:i + OCSVM_BLOQUE])
            for i in range(0, len(X), OCSVM_BLOQUE))
        return -np.concatenate(bloques)

    raise ValueError(tipo)


def combinaciones(rejilla):
    claves = list(rejilla)
    return [dict(zip(claves, v)) for v in itertools.product(*rejilla.values())]


def evaluar_config(nombre, p, X_aj, X_val, y_val, n_jobs):
    inicio = time.perf_counter()
    art = ajustar(nombre, p, X_aj, n_jobs=n_jobs)
    variantes = ([dict(p, distancia=d) for d in ('absoluta', 'relativa')]
                 if nombre == 'kmeans' else [p])
    filas = []
    for v in variantes:
        art['params'] = v
        s = puntuar(art, X_val, n_jobs=n_jobs)
        filas.append({**v, **metricas(y_val, s), 'segundos': time.perf_counter() - inicio})
    return filas


def buscar(nombre, d, rejilla):
    configs = combinaciones(rejilla)
    X_aj = d['ajuste_entreno']

    if nombre == 'ocsvm':
        g = gamma_mediana(X_aj)
        print(f'  gamma base: {g:.5f}')
        configs = [dict(c, gamma=c['gamma_factor'] * g) for c in configs]
        resultados = Parallel(n_jobs=N_JOBS, verbose=0)(
            delayed(evaluar_config)(nombre, c, X_aj, d['val'], d['y_val'], 1)
            for c in configs)
    else:
        resultados = []
        for i, c in enumerate(configs, 1):
            resultados.append(evaluar_config(nombre, c, X_aj, d['val'], d['y_val'], N_JOBS))
            print(f'  [{i}/{len(configs)}] {c}  PR-AUC={max(f["pr_auc"] for f in resultados[-1]):.3f}')

    tabla = (pd.DataFrame([f for filas in resultados for f in filas])
             .sort_values(['pr_auc', COL_TOP], ascending=False)
             .reset_index(drop=True))
    base = d['y_val'].mean()
    tabla['lift_pr'] = tabla['pr_auc'] / base

    print(f'\n  {len(tabla)} combinaciones probadas. Mejores 5 en validación '
          f'(base rate {base * 100:.2f} %):')
    print(tabla.head(5).round(4).to_string(index=False))
    return tabla


def params_fila(tabla, nombre, rejilla, fila=0):
    claves = list(rejilla) + {'kmeans': ['distancia'], 'ocsvm': ['gamma']}.get(nombre, [])
    r = tabla.iloc[[fila]].to_dict('records')[0]
    return {k: (r[k].item() if isinstance(r[k], np.generic) else r[k]) for k in claves}


def estabilidad(nombre, p, d):
    aps = [average_precision_score(d['y_val'],
                                   puntuar(ajustar(nombre, p, d['ajuste_entreno'], semilla=s),
                                           d['val'], N_JOBS))
           for s in SEMILLAS_ESTABILIDAD]
    print(f'  Estabilidad (PR-AUC val con {len(aps)} semillas): '
          f'{np.mean(aps):.3f} ± {np.std(aps):.3f}   [{min(aps):.3f} - {max(aps):.3f}]')
    return float(np.mean(aps)), float(np.std(aps))


def remuestras_bootstrap(y, n, semilla=SEMILLA):
    rng = np.random.default_rng(semilla)
    res = []
    while len(res) < n:
        i = rng.integers(0, len(y), len(y))
        if y[i].any():
            res.append(i)
    return res


def bootstrap_val(nombre, tabla, rejilla, d, n_boot=N_BOOTSTRAP, n_top=N_TOP_BOOTSTRAP):
    n_top = min(n_top, len(tabla))
    y = d['y_val']
    params = [params_fila(tabla, nombre, rejilla, i) for i in range(n_top)]
    scores = [puntuar(ajustar(nombre, p, d['ajuste_entreno']), d['val'], N_JOBS) for p in params]

    remuestras = remuestras_bootstrap(y, n_boot)
    aps = np.array([[average_precision_score(y[i], s[i]) for s in scores]
                    for i in remuestras])              

    filas = []
    for j in range(n_top):
        dif = aps[:, 0] - aps[:, j]
        dif_inf, dif_sup = np.percentile(dif, [2.5, 97.5])
        if j == 0:
            veredicto = 'ganadora'
        elif dif_inf > 0:
            veredicto = 'peor'
        elif dif_sup < 0:
            veredicto = 'mejor (!)'
        else:
            veredicto = 'empate'
        ic_inf, ic_sup = np.percentile(aps[:, j], [2.5, 97.5])
        filas.append({'posicion': j + 1, 'params': json.dumps(params[j], ensure_ascii=False),
                      'pr_auc': float(tabla['pr_auc'].iloc[j]),
                      'ic95_inf': float(ic_inf), 'ic95_sup': float(ic_sup),
                      'dif_inf': float(dif_inf), 'dif_sup': float(dif_sup),
                      'veredicto': veredicto})
    res = pd.DataFrame(filas)

    print(f'\n  Bootstrap pareado en validación ({n_boot} remuestras, '
          f'dif = PR-AUC ganadora - PR-AUC fila):')
    print(res.round(4).to_string(index=False))
    n_empates = (res['veredicto'] == 'empate').sum()
    if n_empates:
        print(f'  {n_empates} combinación(es) empatan con la ganadora')
    return res



def guardar_scores(carpeta, nombre, parte, indice, s, ref, art=None, X=None):
    df = pd.DataFrame({'score': s,
                       'score_norm': normalizar(s, ref['mediana'], ref['p99']),
                       'percentil': percentil(ref['cuantiles'], s)}, index=indice)
    if art is not None and art['tipo'] == 'kmeans':
        df['cluster'] = art['modelo'].predict(X)
    df.to_parquet(os.path.join(carpeta, f'{nombre}_scores_{parte}.parquet'))


def entrenar_modelo(nombre, d, rejilla, carpeta, n_boot):
    print('\n' + '=' * 78)
    print(f'{nombre.upper()}  -  búsqueda de hiperparámetros')
    print('=' * 78)
    inicio = time.perf_counter()

    tabla = buscar(nombre, d, rejilla)
    tabla.to_csv(os.path.join(carpeta, f'{nombre}_busqueda.csv'), index=False)
    params = params_fila(tabla, nombre, rejilla, 0)
    print(f'\n  Ganadora: {params}')
    media, desv = estabilidad(nombre, params, d)

    empates = []
    if n_boot:
        boot = bootstrap_val(nombre, tabla, rejilla, d, n_boot=n_boot)
        boot.to_csv(os.path.join(carpeta, f'{nombre}_bootstrap.csv'), index=False)
        empates = [json.loads(p) for p in boot.loc[boot['veredicto'] == 'empate', 'params']]

    art_aj = ajustar(nombre, params, d['ajuste_entreno'])
    ref_aj = referencia(puntuar(art_aj, d['ajuste_entreno'], N_JOBS))
    guardar_scores(carpeta, nombre, 'val', d['idx_val'], puntuar(art_aj, d['val'], N_JOBS),
                   ref_aj, art_aj, d['val'])

    art = ajustar(nombre, params, d['ajuste_train'])
    s_train = puntuar(art, d['train'], N_JOBS)
    s_test = puntuar(art, d['test'], N_JOBS)
    s_ref = s_train if d['ajuste_train'] is d['train'] else puntuar(art, d['ajuste_train'], N_JOBS)
    ref = referencia(s_ref)
    guardar_scores(carpeta, nombre, 'train', d['idx_train'], s_train, ref, art, d['train'])
    guardar_scores(carpeta, nombre, 'test', d['idx_test'], s_test, ref, art, d['test'])

    art.update(referencia=ref, columnas=d['columnas'])
    joblib.dump(art, os.path.join(carpeta, f'{nombre}.joblib'))

    sobre_p99 = (s_test > ref['p99']).mean() * 100
    print(f'  Test por encima del p99 del train: {sobre_p99:.2f} %  (≈1 % si no hay deriva)')
    print(f'  Tiempo total: {time.perf_counter() - inicio:.0f} s')

    mejor = tabla.iloc[0]
    return {'params': params, 'pr_auc_val': float(mejor['pr_auc']),
            'roc_auc_val': float(mejor['roc_auc']), COL_TOP + '_val': float(mejor[COL_TOP]),
            'pr_auc_val_media_semillas': media, 'pr_auc_val_std_semillas': desv,
            'bootstrap_n': n_boot, 'bootstrap_empates_con_ganadora': empates,
            'combinaciones_probadas': len(tabla), 'test_sobre_p99_train_%': round(sobre_p99, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('modelos', nargs='*', help=f'cualquiera de {MODELOS} (por defecto, todos)')
    parser.add_argument('--rapido', action='store_true', help='rejillas reducidas')
    parser.add_argument('--sin-fraudes', action='store_true',
                        help='quitar los fraudes confirmados de los datos de ajuste')
    parser.add_argument('--bootstrap', type=int, default=N_BOOTSTRAP,
                        help=f'remuestras del bootstrap en validación (0 = no hacerlo; '
                             f'por defecto {N_BOOTSTRAP})')
    args = parser.parse_args()
    desconocidos = set(args.modelos) - set(MODELOS)
    if desconocidos:
        parser.error(f'modelos desconocidos: {sorted(desconocidos)}. Opciones: {MODELOS}')
    args.modelos = args.modelos or MODELOS
    rejillas = REJILLAS_RAPIDAS if args.rapido else REJILLAS

    carpeta = (CARPETA_MODELOS + ('_rapido' if args.rapido else '')
               + ('_sinfraude' if args.sin_fraudes else ''))
    os.makedirs(carpeta, exist_ok=True)
    print(f'Salidas en: {carpeta}/')

    d = cargar(sin_fraudes=args.sin_fraudes)

    ruta_resumen = os.path.join(carpeta, 'resumen_entrenamiento.json')
    resumen = {}
    if os.path.exists(ruta_resumen):
        with open(ruta_resumen, encoding='utf-8') as f:
            resumen = json.load(f)

    for nombre in args.modelos:
        resumen[nombre] = entrenar_modelo(nombre, d, rejillas[nombre], carpeta, args.bootstrap)
        resumen[nombre].update(rejilla='rapida' if args.rapido else 'completa',
                               sin_fraudes=args.sin_fraudes)
        with open(ruta_resumen, 'w', encoding='utf-8') as f:
            json.dump(resumen, f, indent=2, ensure_ascii=False)

    print('\n' + '=' * 78)
    print('RESUMEN (validación)')
    print('=' * 78)
    print(pd.DataFrame({m: {'PR-AUC': r['pr_auc_val'], 'ROC-AUC': r['roc_auc_val'],
                            COL_TOP: r[COL_TOP + '_val'],
                            'empates': len(r.get('bootstrap_empates_con_ganadora', [])),
                            'params': r['params']}
                        for m, r in resumen.items()}).T.to_string())


if __name__ == '__main__':
    main()