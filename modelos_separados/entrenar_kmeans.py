import os
import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.metrics import (adjusted_rand_score, calinski_harabasz_score,
                             davies_bouldin_score, silhouette_score)

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

K = 3                 # elegido con silueta, Calinski-Harabasz y el codo de la inercia
SEMILLA = 42
N_INIT = 10
VERBOSE = 1           # 1 para ver las iteraciones de cada inicialización, 0 para callarlo

DIAGNOSTICO_K = True                        # recorre varios k; ponlo a False si ya está decidido
KS = range(2, 9)
SEMILLAS_ESTABILIDAD = [0, 7, 123, 2024, 99999]


def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    print(f'Train: {X_train.shape}   Test: {X_test.shape}')
    return X_train, X_test


def diagnostico_k(X):
    filas = []
    for k in KS:
        m = KMeans(n_clusters=k, random_state=SEMILLA, n_init=N_INIT).fit(X)
        filas.append({
            'k': k,
            'inercia': round(m.inertia_),
            'silueta': round(silhouette_score(X, m.labels_, sample_size=5000,
                                              random_state=SEMILLA), 3),
            'calinski': round(calinski_harabasz_score(X, m.labels_)),
            'davies_bouldin': round(davies_bouldin_score(X, m.labels_), 3),
            'cluster_mas_pequeno': int(pd.Series(m.labels_).value_counts().min()),
        })
    print('\nDiagnóstico para elegir k:')
    print(pd.DataFrame(filas).to_string(index=False))


def entrenar(X_train):
    modelo = KMeans(n_clusters=K, random_state=SEMILLA, n_init=N_INIT, verbose=VERBOSE)
    modelo.fit(X_train)   # sin etiquetas: KMeans solo ve las variables

    tam = pd.Series(modelo.labels_).value_counts().sort_index()
    print(f'\nKMeans entrenado con k={K} sobre {X_train.shape[1]} variables')
    print(f'  Inercia: {modelo.inertia_:,.0f}')
    print(f'  Iteraciones hasta converger: {modelo.n_iter_}')
    print('  Tamaño de cada cluster:')
    for cluster, n in tam.items():
        print(f'    cluster {cluster}: {n:,} ({n / len(X_train) * 100:.1f} %)')
    return modelo


def calidad_interna(X_train, modelo):
    etiquetas = modelo.labels_

    print('\nCalidad de la partición:')
    print(f'  Silueta          : '
          f'{silhouette_score(X_train, etiquetas, sample_size=5000, random_state=SEMILLA):.3f}'
          '   (-1 a 1, más alto mejor)')
    print(f'  Calinski-Harabasz: {calinski_harabasz_score(X_train, etiquetas):,.0f}'
          '        (más alto mejor)')
    print(f'  Davies-Bouldin   : {davies_bouldin_score(X_train, etiquetas):.3f}'
          '        (más bajo mejor)')

    # ¿La partición es reproducible o depende de dónde caigan los centroides iniciales?
    aris = [adjusted_rand_score(etiquetas,
                                KMeans(n_clusters=K, random_state=s, n_init=N_INIT)
                                .fit(X_train).labels_)
            for s in SEMILLAS_ESTABILIDAD]
    print(f'\nEstabilidad frente a la semilla (ARI con {len(aris)} semillas distintas):')
    print(f'  media {np.mean(aris):.3f}   mínimo {np.min(aris):.3f}')
    print('  1.0 = siempre la misma partición | 0.0 = tan distinta como al azar')


def puntuar(modelo, X):
    return modelo.transform(X).min(axis=1)


def normalizar(distancias_train, distancias):
    referencia = np.sort(distancias_train)
    posicion = np.searchsorted(referencia, distancias, side='right')
    return posicion / len(referencia)


def guardar(modelo, X_train, X_test, dist_train, dist_test, score_train, score_test):
    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    joblib.dump(modelo, os.path.join(CARPETA_MODELOS, 'kmeans.joblib'))
    joblib.dump(np.sort(dist_train), os.path.join(CARPETA_MODELOS, 'kmeans_referencia.joblib'))

    for nombre, X, dist, score in [('train', X_train, dist_train, score_train),
                                   ('test', X_test, dist_test, score_test)]:
        pd.DataFrame({'cluster': modelo.predict(X), 'distancia': dist, 'score': score},
                     index=X.index).to_parquet(
            os.path.join(CARPETA_MODELOS, f'kmeans_scores_{nombre}.parquet'))

    print(f'\nGuardado en "{CARPETA_MODELOS}/": kmeans.joblib, kmeans_referencia.joblib '
          'y los scores de train y test')


def main():
    X_train, X_test = cargar()

    if DIAGNOSTICO_K:
        diagnostico_k(X_train)

    modelo = entrenar(X_train)
    calidad_interna(X_train, modelo)

    dist_train = puntuar(modelo, X_train)
    dist_test = puntuar(modelo, X_test)
    score_train = normalizar(dist_train, dist_train)
    score_test = normalizar(dist_train, dist_test)

    print('\nDistancia al centroide (train):')
    print(pd.Series(dist_train).describe().round(2).to_string())
    print(f'\nTest por encima del percentil 99 del train: '
          f'{(score_test >= 0.99).sum():,} de {len(score_test):,} '
          f'({(score_test >= 0.99).mean() * 100:.2f} %)')

    guardar(modelo, X_train, X_test, dist_train, dist_test, score_train, score_test)


if __name__ == '__main__':
    main()