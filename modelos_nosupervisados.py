import argparse
import itertools
import json
import os
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.ensemble import IsolationForest
from sklearn.metrics import average_precision_score
from sklearn.svm import OneClassSVM

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

# Validación = el tramo MÁS RECIENTE del train (partición temporal, igual que el autoencoder).
# Tiene que ser el mismo valor para TODOS los modelos: la evaluación exige las mismas filas.
PROPORCION_VAL = 0.15

SEMILLA = 42
SEMILLAS_ESTABILIDAD = [0, 1, 2, 3, 4]   # para comprobar que el resultado no depende del azar
OCSVM_MAX_FILAS = 10_000                 # OCSVM es muy lento con más filas
KMEANS_N_INIT = 10

# Configuraciones que se prueban de cada modelo (se elige la de mejor PR-AUC en validación)
REJILLAS = {
    'iforest': {'n_estimators': [200, 400], 'max_samples': [256, 1024], 'max_features': [0.5, 1.0]},
    'kmeans': {'k': [2, 3, 5, 8, 12]},
    'ocsvm': {'nu': [0.01, 0.05], 'gamma': ['scale', 0.01]},
}
MODELOS = list(REJILLAS)

# Qué partes se guardan en anomaly_scores (la evaluación sigue leyendo los parquets)
PARTES_A_CLICKHOUSE = ['test']



def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    if not X_train.index.equals(info_train.index):
        raise ValueError('X_train e info_train no tienen las mismas filas. '
                         'Vuelve a ejecutar preparacion_datos_ml.py')

    corte = int(len(X_train) * (1 - PROPORCION_VAL))
    X_fit, X_val = X_train.iloc[:corte], X_train.iloc[corte:]
    y_val = info_train['es_fraude'].iloc[corte:].astype(int).to_numpy()
    if y_val.sum() == 0:
        raise ValueError('No hay fraudes en validación: no se puede elegir configuración')

    print(f'Ajuste:     {len(X_fit):,} filas (sin etiquetas)')
    print(f'Validación: {len(X_val):,} filas, {y_val.sum()} fraudes (las más recientes del train)')
    print(f'Test:       {len(X_test):,} filas')
    ids = pd.concat([info_train['transaccion_id'], info_test['transaccion_id']])
    return X_train, X_test, X_fit, X_val, y_val, ids

def entrenar(nombre, p, X, semilla):
    """Ajusta un modelo con los parámetros p. X es un array de numpy."""
    if nombre == 'iforest':
        return IsolationForest(n_estimators=p['n_estimators'],
                               max_samples=min(p['max_samples'], len(X)),
                               max_features=p['max_features'],
                               random_state=semilla, n_jobs=-1).fit(X)
    if nombre == 'kmeans':
        return KMeans(n_clusters=p['k'], n_init=KMEANS_N_INIT, random_state=semilla).fit(X)
    if nombre == 'ocsvm':
        rng = np.random.default_rng(semilla)
        filas = rng.choice(len(X), size=min(OCSVM_MAX_FILAS, len(X)), replace=False)
        return OneClassSVM(kernel='rbf', nu=p['nu'], gamma=p['gamma'],
                           cache_size=1000).fit(X[filas])
    raise ValueError(f'Modelo desconocido: {nombre}')


def puntuar(nombre, modelo, X):
    """Devuelve (score, extra). Score más alto = más anómalo."""
    if nombre == 'iforest':
        return -modelo.score_samples(X), {}
    if nombre == 'kmeans':
        distancias = modelo.transform(X)              # distancia a cada centroide
        return distancias.min(axis=1), {'cluster': distancias.argmin(axis=1)}
    if nombre == 'ocsvm':
        return -modelo.decision_function(X), {}
    raise ValueError(f'Modelo desconocido: {nombre}')


def referencia(score_ajuste):
    """Mediana y percentil 99 de los scores del ajuste, para normalizar."""
    return {'mediana': float(np.median(score_ajuste)),
            'p99': float(np.percentile(score_ajuste, 99))}


def normalizar(s, mediana, p99):
    """0 = transacción típica, 1 = percentil 99 del ajuste. No cambia el orden (ni la PR-AUC)."""
    return (s - mediana) / max(p99 - mediana, 1e-12)


def combinaciones(rejilla):
    claves = list(rejilla)
    return [dict(zip(claves, valores)) for valores in itertools.product(*rejilla.values())]


def buscar(nombre, X_fit, X_val, y_val):
    candidatos = combinaciones(REJILLAS[nombre])
    print(f'\nBuscando configuración ({len(candidatos)} combinaciones):')
    mejor_p, mejor_ap = None, -1.0
    for p in candidatos:
        modelo = entrenar(nombre, p, X_fit, SEMILLA)
        s_val, _ = puntuar(nombre, modelo, X_val)
        ap = average_precision_score(y_val, s_val)
        print(f'  {str(p):<70} PR-AUC val {ap:.4f}')
        if ap > mejor_ap:
            mejor_p, mejor_ap = p, ap
    print(f'  -> elegida: {mejor_p}  (PR-AUC val {mejor_ap:.4f})')
    return mejor_p, mejor_ap, len(candidatos)


def estabilidad(nombre, p, X_fit, X_val, y_val):
    aps = []
    for semilla in SEMILLAS_ESTABILIDAD:
        modelo = entrenar(nombre, p, X_fit, semilla)
        s_val, _ = puntuar(nombre, modelo, X_val)
        aps.append(average_precision_score(y_val, s_val))
    media, std = float(np.mean(aps)), float(np.std(aps))
    print(f'Estabilidad con {len(aps)} semillas: PR-AUC val {media:.4f} ± {std:.4f}')
    return media, std



def guardar_scores(nombre, modelo, partes, ref):
    resultado = {}
    for parte, X in partes.items():
        s, extra = puntuar(nombre, modelo, X.to_numpy())
        df = pd.DataFrame({'score': s, 'score_norm': normalizar(s, ref['mediana'], ref['p99'])},
                          index=X.index)
        for col, valores in extra.items():
            df[col] = valores
        df.to_parquet(os.path.join(CARPETA_MODELOS, f'{nombre}_scores_{parte}.parquet'))
        resultado[parte] = df
    return resultado


def conectar_clickhouse():
    import clickhouse_connect
    from BD_VACIAS import DATABASE, HOST, PASSWORD, PORT, USER
    try:
        client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                               password=PASSWORD, database=DATABASE)
        client.command('SELECT 1')
    except Exception as e:
        raise SystemExit(f'No se puede conectar a ClickHouse ({e}).\n'
                         'Los parquets SÍ se han guardado. Levanta ClickHouse o usa --sin-clickhouse.')
    tipo_id = next(t for nombre, t, *_ in client.query('DESCRIBE TABLE transaccion').result_rows
                   if nombre == 'transaccion_id')
    client.command(f'''
        CREATE TABLE IF NOT EXISTS anomaly_scores (
            transaccion_id  {tipo_id},
            model           LowCardinality(String),
            score           Float64,
            score_pct       Float32,
            run_id          String,
            ts              DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY (model, run_id, transaccion_id)
    ''')
    return client


def guardar_en_clickhouse(client, nombre, scores, ids, run_id):
    for parte in PARTES_A_CLICKHOUSE:
        df = scores[parte]
        tabla = pd.DataFrame({
            'transaccion_id': ids.loc[df.index].to_numpy(dtype=np.uint32),
            'model': nombre,
            'score': df['score'].to_numpy(dtype=np.float64),
            'score_pct': df['score'].rank(pct=True).to_numpy(dtype=np.float32),
            'run_id': run_id,
        })
        client.insert_df('anomaly_scores', tabla)
        print(f'anomaly_scores: {len(tabla):,} filas de {parte} (run_id {run_id})')


def actualizar_resumen(nombre, datos):
    ruta = os.path.join(CARPETA_MODELOS, 'resumen_entrenamiento.json')
    resumen = {}
    if os.path.exists(ruta):
        with open(ruta, encoding='utf-8') as fh:
            resumen = json.load(fh)
    resumen[nombre] = datos
    with open(ruta, 'w', encoding='utf-8') as fh:
        json.dump(resumen, fh, indent=2, ensure_ascii=False)



def procesar(nombre, X_train, X_test, X_fit, X_val, y_val, ids, run_id, client):
    print('\n' + '=' * 90)
    print(f'MODELO: {nombre}')
    print('=' * 90)
    A_fit, A_val = X_fit.to_numpy(), X_val.to_numpy()

    mejor_p, mejor_ap, n_comb = buscar(nombre, A_fit, A_val, y_val)
    media, std = estabilidad(nombre, mejor_p, A_fit, A_val, y_val)

    modelo = entrenar(nombre, mejor_p, A_fit, SEMILLA)
    ref = referencia(puntuar(nombre, modelo, A_fit)[0])

    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    joblib.dump(modelo, os.path.join(CARPETA_MODELOS, f'{nombre}.joblib'))
    scores = guardar_scores(nombre, modelo, {'train': X_train, 'val': X_val, 'test': X_test}, ref)
    actualizar_resumen(nombre, {
        'mejores_parametros': mejor_p,
        'combinaciones_probadas': n_comb,
        'pr_auc_val': float(mejor_ap),
        'pr_auc_val_media_semillas': media,
        'pr_auc_val_std_semillas': std,
        'referencia_normalizacion': ref,
        'proporcion_val': PROPORCION_VAL,
        'filas_ajuste': len(X_fit),
        'filas_validacion': len(X_val),
        'semilla': SEMILLA,
        'run_id': run_id,
    })
    print(f'Guardado en "{CARPETA_MODELOS}/": {nombre}.joblib y {nombre}_scores_train/val/test.parquet')
    if client is not None:
        guardar_en_clickhouse(client, nombre, scores, ids, run_id)


def main():
    parser = argparse.ArgumentParser(description='Entrena los modelos no supervisados clásicos')
    parser.add_argument('modelos', nargs='*', default='todos', choices=MODELOS + ['todos'])
    parser.add_argument('--sin-clickhouse', action='store_true',
                        help='no guardar los scores en anomaly_scores')
    args = parser.parse_args()
    elegidos = MODELOS if 'todos' in args.modelos else list(dict.fromkeys(args.modelos))

    run_id = f'clasicos_{datetime.now():%Y%m%d_%H%M%S}'   # el mismo para toda la ejecución
    print(f'run_id: {run_id}')
    client = None if args.sin_clickhouse else conectar_clickhouse()

    X_train, X_test, X_fit, X_val, y_val, ids = cargar()
    for nombre in elegidos:
        procesar(nombre, X_train, X_test, X_fit, X_val, y_val, ids, run_id, client)


if __name__ == '__main__':
    main()