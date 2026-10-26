"""Piezas comunes del modelo supervisado HGB + GMM.

El modelo tiene dos pasos:

1. Un GaussianMixture (GMM) aprende cómo son las transacciones del ajuste usando las 36
   variables del modelo y ``n_comercios_cliente_3h`` estandarizada. Su score de rareza
   (``-score_samples``) es una variable más para el clasificador.
2. Un HistGradientBoostingClassifier aprende, con las etiquetas de fraude confirmadas, a
   ordenar las transacciones. Recibe las 36 variables y el score del GMM.

Este módulo no lee archivos ni config.yaml: solo construye/puntúa un diccionario de modelo.
Así lo pueden usar el entrenamiento, el scoring diario y el recalibrador sin duplicar código.
"""
import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler


def _matriz(X, columnas):
    """Devuelve una matriz float con las columnas del entrenamiento, en su orden."""
    if hasattr(X, 'columns'):
        return X.loc[:, columnas].to_numpy(dtype=float)
    X = np.asarray(X, dtype=float)
    if X.shape[1] != len(columnas):
        raise ValueError(f'El modelo espera {len(columnas)} variables y ha recibido {X.shape[1]}')
    return X


def _extra(extra):
    """Una sola columna float (n_comercios_cliente_3h)."""
    extra = np.asarray(extra, dtype=float)
    if extra.ndim == 1:
        return extra.reshape(-1, 1)
    if extra.ndim == 2 and extra.shape[1] == 1:
        return extra
    raise ValueError(f'La variable extra debe tener una columna y tiene {extra.shape[1]}')


def construir(X, extra, y, parametros_gmm, parametros_hgb, semilla, columnas=None):
    """Ajusta el GMM y el HGB.

    X: matriz o DataFrame con las 36 variables ya escaladas.
    extra: n_comercios_cliente_3h sin escalar (una fila por transacción).
    y: etiquetas conocidas (1 = fraude confirmado).
    """
    columnas = list(columnas if columnas is not None else getattr(X, 'columns', []))
    if not columnas:
        raise ValueError('Faltan los nombres de las columnas del modelo')
    X = _matriz(X, columnas)
    extra = _extra(extra)
    y = np.asarray(y, dtype=int)

    escalador_extra = StandardScaler().fit(extra)
    extra_esc = escalador_extra.transform(extra)
    Z = np.hstack([X, extra_esc])

    gmm = GaussianMixture(**parametros_gmm, random_state=semilla).fit(Z)
    score_gmm = -gmm.score_samples(Z)

    hgb = HistGradientBoostingClassifier(**parametros_hgb, random_state=semilla).fit(
        np.hstack([X, score_gmm[:, None]]), y)

    return {
        'gmm': gmm,
        'escalador_extra': escalador_extra,
        'hgb': hgb,
        'columnas': columnas,
        'variables_extra': ['n_comercios_cliente_3h'],
        'parametros_gmm': dict(parametros_gmm),
        'parametros_hgb': dict(parametros_hgb),
        'semilla': int(semilla),
    }


def puntuar(modelo, X, extra):
    """Devuelve (score de fraude, score del GMM). Score más alto = más sospechoso."""
    X = _matriz(X, modelo['columnas'])
    extra = _extra(extra)
    extra_esc = modelo['escalador_extra'].transform(extra)

    score_gmm = -modelo['gmm'].score_samples(np.hstack([X, extra_esc]))
    score = modelo['hgb'].predict_proba(np.hstack([X, score_gmm[:, None]]))[:, 1]
    return score, score_gmm


def cargar(ruta):
    """Carga el diccionario guardado por entrenar_modelo_supervisado.py."""
    modelo = joblib.load(ruta)
    faltan = {'gmm', 'escalador_extra', 'hgb', 'columnas'} - set(modelo)
    if faltan:
        raise ValueError(f'{ruta.name}: faltan piezas del modelo ({", ".join(sorted(faltan))})')
    return modelo
