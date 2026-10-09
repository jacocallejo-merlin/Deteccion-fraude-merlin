import os
import time
import joblib
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.metrics import pairwise_distances
from sklearn.svm import OneClassSVM

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

NU = 0.01           
GAMMA = None          
N_MUESTRA = 20000    
SEMILLA = 42

DIAGNOSTICO = True    
GAMMAS_DIAG = [0.002, 0.005, 0.01, 0.02, 0.05]
NUS_DIAG = [0.005, 0.01, 0.02, 0.05]
N_DIAG = 5000         
SEMILLAS_ESTABILIDAD = [0, 7, 123, 2024, 99999]


def cargar():
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    print(f'Train: {X_train.shape}   Test: {X_test.shape}')
    return X_train, X_test


def muestra(X, n, semilla):
    return X.sample(min(n, len(X)), random_state=semilla)


def gamma_mediana(X):
    m = muestra(X, 2000, SEMILLA).to_numpy()
    d2 = pairwise_distances(m, metric='sqeuclidean')
    mediana = np.median(d2[np.triu_indices_from(d2, k=1)])
    return 1.0 / mediana


def puntuar(modelo, X):
    return -modelo.decision_function(X)


def diagnostico(X):
    X_ref = muestra(X, N_DIAG, 999)   # mismas filas para comparar todos los modelos
    filas = []
    for gamma in GAMMAS_DIAG:
        for nu in NUS_DIAG:
            inicio = time.time()
            modelos = [OneClassSVM(kernel='rbf', nu=nu, gamma=gamma).fit(muestra(X, N_DIAG, s))
                       for s in SEMILLAS_ESTABILIDAD]
            segundos = (time.time() - inicio) / len(modelos)
            scores = [puntuar(m, X_ref) for m in modelos]
            rhos = [spearmanr(scores[0], s)[0] for s in scores[1:]]
            filas.append({
                'gamma': gamma,
                'nu': nu,
                'frac_vectores_soporte': round(np.mean([len(m.support_) for m in modelos]) / N_DIAG, 3),
                'pct_marcadas': round(np.mean([(m.predict(X_ref) == -1).mean() for m in modelos]) * 100, 2),
                'estabilidad_media': round(float(np.mean(rhos)), 3),
                'estabilidad_min': round(float(np.min(rhos)), 3),
                'segundos': round(segundos, 1),
            })
        print(f'  gamma={gamma} hecho')
    print('\nDiagnóstico para elegir gamma y nu (sin etiquetas):')
    print(pd.DataFrame(filas).to_string(index=False))


def entrenar(X_train, gamma):
    datos = muestra(X_train, N_MUESTRA, SEMILLA)
    inicio = time.time()
    modelo = OneClassSVM(kernel='rbf', nu=NU, gamma=gamma)
    modelo.fit(datos)   # sin etiquetas: el OCSVM solo ve las variables
    print(f'\nOne-Class SVM entrenado con nu={NU}, gamma={gamma:.5f} sobre {len(datos):,} filas '
          f'y {X_train.shape[1]} variables ({time.time() - inicio:.1f} s)')
    print(f'  Vectores soporte: {len(modelo.support_):,} '
          f'({len(modelo.support_) / len(datos) * 100:.1f} % de la muestra)')
    return modelo


def normalizar(puntuacion_train, puntuacion):
    referencia = np.sort(puntuacion_train)
    posicion = np.searchsorted(referencia, puntuacion, side='right')
    return posicion / len(referencia)


def guardar(modelo, X_train, X_test, punt_train, punt_test, score_train, score_test):
    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    joblib.dump(modelo, os.path.join(CARPETA_MODELOS, 'ocsvm.joblib'))
    joblib.dump(np.sort(punt_train), os.path.join(CARPETA_MODELOS, 'ocsvm_referencia.joblib'))

    for nombre, X, punt, score in [('train', X_train, punt_train, score_train),
                                   ('test', X_test, punt_test, score_test)]:
        pd.DataFrame({'puntuacion': punt, 'score': score}, index=X.index).to_parquet(
            os.path.join(CARPETA_MODELOS, f'ocsvm_scores_{nombre}.parquet'))

    print(f'\nGuardado en "{CARPETA_MODELOS}/": ocsvm.joblib, ocsvm_referencia.joblib '
          'y los scores de train y test')


def main():
    X_train, X_test = cargar()

    gamma_med = gamma_mediana(X_train)
    gamma = GAMMA if GAMMA is not None else gamma_med
    print(f'\nGamma por heurística de la mediana: {gamma_med:.5f}'
          f'   (gamma="scale" de sklearn sería {1 / (X_train.shape[1] * X_train.to_numpy().var()):.5f})')

    if DIAGNOSTICO:
        diagnostico(X_train)

    modelo = entrenar(X_train, gamma)

    punt_train = puntuar(modelo, X_train)
    punt_test = puntuar(modelo, X_test)
    score_train = normalizar(punt_train, punt_train)
    score_test = normalizar(punt_train, punt_test)

    print('\nPuntuación de anomalía (train):')
    print(pd.Series(punt_train).describe().round(3).to_string())
    print(f'\nTrain marcado como anómalo por el modelo: {(modelo.predict(X_train) == -1).mean() * 100:.2f} %'
          f'   (nu = {NU * 100:.2f} %)')
    print(f'Test por encima del percentil 99 del train: '
          f'{(score_test >= 0.99).sum():,} de {len(score_test):,} '
          f'({(score_test >= 0.99).mean() * 100:.2f} %)')

    guardar(modelo, X_train, X_test, punt_train, punt_test, score_train, score_test)


if __name__ == '__main__':
    main()