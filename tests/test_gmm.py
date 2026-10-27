"""Los dos modelos con GMM: el GMM como detector no supervisado (modelos_nosupervisados.py, como KMeans o DBSCAN)
y el supervisado HGB + GMM (modelo_supervisado.py). Con datos sintéticos: no necesita ClickHouse ni el pipeline."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import modelo_supervisado
import modelos_nosupervisados as mns
from evaluar_modelos_nosupervisados import MODELOS as EVALUADOS, SUPERVISADOS
from recalibrar_modelo import DETECTORES

SEMILLA = 42
P = {"n_components": 3, "covariance_type": "full", "reg_covar": 0.003}


def datos(n=1500, raros=15, semilla=0):
    """Pagos normales en tres nubes y unos pocos lejos de todas (los "raros", al final)."""
    rng = np.random.default_rng(semilla)
    centros = np.array([[0, 0, 0, 0], [5, 5, 0, 0], [0, 5, 5, 0]], dtype=float)
    normales = centros[rng.integers(0, 3, n)] + rng.normal(0, 0.6, (n, 4))
    lejanos = rng.normal(0, 0.6, (raros, 4)) + np.array([12, -8, 10, 9])
    return np.vstack([normales, lejanos]), np.r_[np.zeros(n), np.ones(raros)].astype(int)


# --------------------------------------------------------------------------- GMM no supervisado
def test_gmm_esta_en_todo_el_pipeline():
    assert "gmm" in mns.MODELOS                     # se entrena y se puntúa como los clásicos (config.yaml)
    assert "gmm" in EVALUADOS and "hgb_gmm" in EVALUADOS and SUPERVISADOS == {"hgb_gmm"}
    assert "gmm" in DETECTORES and "hgb_gmm" not in DETECTORES   # el recalibrador no aprende de otro supervisado


def test_gmm_pone_arriba_lo_raro():
    X, y = datos()
    modelo = mns.entrenar("gmm", P, X[y == 0], SEMILLA)          # aprende solo con lo normal, sin etiquetas
    s, extra = mns.puntuar("gmm", modelo, X)
    assert s.shape == (len(X),) and set(np.unique(extra["cluster"])) <= set(range(P["n_components"]))
    assert s[y == 1].min() > np.percentile(s[y == 0], 99)          # los lejanos, más raros que el 99 % de lo normal


def test_gmm_misma_semilla_mismo_resultado():
    X, _ = datos()
    a = mns.puntuar("gmm", mns.entrenar("gmm", P, X, SEMILLA), X)[0]
    b = mns.puntuar("gmm", mns.entrenar("gmm", P, X, SEMILLA), X)[0]
    assert np.allclose(a, b)


def test_gmm_rejilla_de_config():
    for p in mns.combinaciones(mns.REJILLAS["gmm"]):
        assert {"n_components", "covariance_type"} <= set(p)


# --------------------------------------------------------------------------- HGB + GMM supervisado
def test_supervisado_aprende_de_los_fraudes_confirmados():
    X, y = datos(n=3000, raros=60, semilla=1)
    columnas = [f"v{i}" for i in range(X.shape[1])]
    Xdf = pd.DataFrame(X, columns=columnas)
    extra = np.random.default_rng(2).integers(1, 4, len(X))         # n_comercios_cliente_3h
    gmm = {"n_components": 3, "covariance_type": "tied", "reg_covar": 0.003, "max_iter": 200, "n_init": 1}
    hgb = {"learning_rate": 0.1, "max_iter": 100, "max_leaf_nodes": 7, "min_samples_leaf": 10}
    modelo = modelo_supervisado.construir(Xdf, extra, y, gmm, hgb, SEMILLA)
    s, s_gmm = modelo_supervisado.puntuar(modelo, Xdf, extra)
    assert s.shape == s_gmm.shape == (len(X),) and ((0 <= s) & (s <= 1)).all()
    assert s[y == 1].mean() > s[y == 0].mean() + 0.5
    otra = modelo_supervisado.construir(Xdf, extra, y, gmm, hgb, SEMILLA)   # reproducible con la misma semilla
    assert np.allclose(s, modelo_supervisado.puntuar(otra, Xdf, extra)[0])
