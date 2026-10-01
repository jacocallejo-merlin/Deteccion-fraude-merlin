"""
Entrenamiento de detectores de anomalías: Isolation Forest, KMeans y One-Class SVM.

Esquema temporal (el test NUNCA se usa para decidir nada):

    X_train (70 % más antiguo)                          X_test (30 % más reciente)
    [ entreno (80 %)           | validación (20 %) ]    [ no se toca hasta evaluar ]

  1. Cada combinación de hiperparámetros se ajusta SIN etiquetas sobre "entreno".
  2. Se puntúa "validación" y se mide la PR-AUC con es_fraude -> gana la mejor.
     (Es lo realista: el banco tiene fraudes confirmados del pasado para elegir
      el modelo, pero el modelo en sí no aprende de ellos).
  3. La ganadora se reentrena con TODO el train y se puntúan train y test.

Salidas en modelos/:
  <modelo>.joblib                       modelo final + referencia para normalizar
  <modelo>_busqueda.csv                 todas las combinaciones probadas
  <modelo>_scores_{val,train,test}.parquet
  resumen_entrenamiento.json            mejores hiperparámetros y métricas de validación

Uso:
  python entrenar_modelos.py                    # los tres modelos
  python entrenar_modelos.py iforest kmeans     # solo algunos
  python entrenar_modelos.py --rapido           # rejillas reducidas para probar
"""
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

REJILLAS_RAPIDAS = {
    'iforest': {'n_estimators': [100], 'max_samples': [256, 1024], 'max_features': [1.0]},
    'kmeans': {'k': [3, 6]},
    'ocsvm': {'gamma_factor': [0.5, 1], 'nu': [0.01]},
}

MODELOS = list(REJILLAS)
COL_TOP = f'recall_top{TOP_PCT:g}%'



def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    if not X_train.index.equals(info_train.index):
        raise ValueError('X_train e info_train no tienen el mismo índice')

    corte = int(len(X_train) * (1 - FRAC_VALIDACION))
    y_val = info_train['es_fraude'].iloc[corte:].astype(int).to_numpy()

    d = {
        'columnas': list(X_train.columns),
        'idx_train': X_train.index, 'idx_test': X_test.index,
        'idx_entreno': X_train.index[:corte], 'idx_val': X_train.index[corte:],
        'train': np.ascontiguousarray(X_train.to_numpy(np.float64)),
        'test': np.ascontiguousarray(X_test.to_numpy(np.float64)),
        'y_val': y_val,
    }
    d['entreno'], d['val'] = d['train'][:corte], d['train'][corte:]

    print(f'Variables: {len(d["columnas"])}')
    print(f'Entreno:    {len(d["entreno"]):>9,} filas   (hasta {info_train["timestamp"].iloc[corte - 1]})')
    print(f'Validación: {len(d["val"]):>9,} filas   ({y_val.sum()} fraudes, {y_val.mean() * 100:.2f} %)')
    print(f'Test:       {len(d["test"]):>9,} filas   (no se usa aquí para decidir nada)')
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


def percentil(referencia_ordenada, s):
    return np.searchsorted(referencia_ordenada, s, side='right') / len(referencia_ordenada)


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

    if nombre == 'ocsvm':
        g = gamma_mediana(d['entreno'])
        print(f'  gamma base: {g:.5f}')
        configs = [dict(c, gamma=c['gamma_factor'] * g) for c in configs]
        resultados = Parallel(n_jobs=N_JOBS, verbose=0)(
            delayed(evaluar_config)(nombre, c, d['entreno'], d['val'], d['y_val'], 1)
            for c in configs)
    else:
        resultados = []
        for i, c in enumerate(configs, 1):
            resultados.append(evaluar_config(nombre, c, d['entreno'], d['val'], d['y_val'], N_JOBS))
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


def mejores_params(tabla, nombre, rejilla):
    claves = list(rejilla) + {'kmeans': ['distancia'], 'ocsvm': ['gamma']}.get(nombre, [])
    fila = tabla.head(1).to_dict('records')[0]   
    return {k: (fila[k].item() if isinstance(fila[k], np.generic) else fila[k]) for k in claves}


def estabilidad(nombre, p, d):
    aps = [average_precision_score(d['y_val'],
                                   puntuar(ajustar(nombre, p, d['entreno'], semilla=s),
                                           d['val'], N_JOBS))
           for s in SEMILLAS_ESTABILIDAD]
    print(f'  Estabilidad (PR-AUC val con {len(aps)} semillas): '
          f'{np.mean(aps):.3f} ± {np.std(aps):.3f}   [{min(aps):.3f} - {max(aps):.3f}]')
    return float(np.mean(aps)), float(np.std(aps))


def referencia(s):
    return {'ordenada': np.sort(s), 'mediana': float(np.median(s)),
            'p99': float(np.quantile(s, 0.99))}


def guardar_scores(nombre, parte, indice, s, ref, art=None, X=None):
    df = pd.DataFrame({'score': s,
                       'score_norm': normalizar(s, ref['mediana'], ref['p99']),
                       'percentil': percentil(ref['ordenada'], s)}, index=indice)
    if art is not None and art['tipo'] == 'kmeans':
        df['cluster'] = art['modelo'].predict(X)
    df.to_parquet(os.path.join(CARPETA_MODELOS, f'{nombre}_scores_{parte}.parquet'))


def entrenar_modelo(nombre, d, rejilla):
    print('\n' + '=' * 78)
    print(f'{nombre.upper()}  -  búsqueda de hiperparámetros')
    print('=' * 78)
    inicio = time.perf_counter()

    tabla = buscar(nombre, d, rejilla)
    tabla.to_csv(os.path.join(CARPETA_MODELOS, f'{nombre}_busqueda.csv'), index=False)
    params = mejores_params(tabla, nombre, rejilla)
    print(f'\n  Ganadora: {params}')
    media, desv = estabilidad(nombre, params, d)

    art_aj = ajustar(nombre, params, d['entreno'])
    ref_aj = referencia(puntuar(art_aj, d['entreno'], N_JOBS))
    guardar_scores(nombre, 'val', d['idx_val'], puntuar(art_aj, d['val'], N_JOBS),
                   ref_aj, art_aj, d['val'])

    art = ajustar(nombre, params, d['train'])
    s_train = puntuar(art, d['train'], N_JOBS)
    s_test = puntuar(art, d['test'], N_JOBS)
    ref = referencia(s_train)
    guardar_scores(nombre, 'train', d['idx_train'], s_train, ref, art, d['train'])
    guardar_scores(nombre, 'test', d['idx_test'], s_test, ref, art, d['test'])

    art.update(referencia=ref, columnas=d['columnas'])  
    joblib.dump(art, os.path.join(CARPETA_MODELOS, f'{nombre}.joblib'))

    sobre_p99 = (s_test > ref['p99']).mean() * 100
    print(f'  Test por encima del p99 del train: {sobre_p99:.2f} %  (≈1 % si no hay deriva)')
    print(f'  Tiempo total: {time.perf_counter() - inicio:.0f} s')

    mejor = tabla.iloc[0]
    return {'params': params, 'pr_auc_val': float(mejor['pr_auc']),
            'roc_auc_val': float(mejor['roc_auc']), COL_TOP + '_val': float(mejor[COL_TOP]),
            'pr_auc_val_media_semillas': media, 'pr_auc_val_std_semillas': desv,
            'combinaciones_probadas': len(tabla), 'test_sobre_p99_train_%': round(sobre_p99, 2)}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('modelos', nargs='*', help=f'cualquiera de {MODELOS} (por defecto, todos)')
    parser.add_argument('--rapido', action='store_true', help='rejillas reducidas')
    args = parser.parse_args()
    desconocidos = set(args.modelos) - set(MODELOS)
    if desconocidos:
        parser.error(f'modelos desconocidos: {sorted(desconocidos)}. Opciones: {MODELOS}')
    args.modelos = args.modelos or MODELOS
    rejillas = REJILLAS_RAPIDAS if args.rapido else REJILLAS

    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    d = cargar()

    ruta_resumen = os.path.join(CARPETA_MODELOS, 'resumen_entrenamiento.json')
    resumen = {}
    if os.path.exists(ruta_resumen):
        with open(ruta_resumen, encoding='utf-8') as f:
            resumen = json.load(f)

    for nombre in args.modelos:
        resumen[nombre] = entrenar_modelo(nombre, d, rejillas[nombre])
        with open(ruta_resumen, 'w', encoding='utf-8') as f:  
            json.dump(resumen, f, indent=2, ensure_ascii=False)

    print('\n' + '=' * 78)
    print('RESUMEN (validación)')
    print('=' * 78)
    print(pd.DataFrame({m: {'PR-AUC': r['pr_auc_val'], 'ROC-AUC': r['roc_auc_val'],
                            COL_TOP: r[COL_TOP + '_val'], 'params': r['params']}
                        for m, r in resumen.items()}).T.to_string())

if __name__ == '__main__':
    main()