import numpy as np
import pandas as pd

from pipeline.preparacion_datos_ml import PROPORCION_TRAIN, dividir, escalar


def datos(n=1000):
    rng = np.random.default_rng(0)
    timestamps = pd.date_range("2026-03-01", periods=n, freq="15min")
    # una variable que crece con el tiempo: si el escalador viera el test, su media en train no sería 0
    X = pd.DataFrame({"a": np.arange(n, dtype=float), "b": rng.normal(size=n)})
    info = pd.DataFrame({"timestamp": timestamps, "es_fraude": rng.random(n) < 0.02,
                         "tipo_fraude": None})
    return X, info


def test_particion_temporal_sin_solape():
    X, info = datos()
    X_train, X_test, info_train, info_test = dividir(X, info)
    assert len(X_train) == int(len(X) * PROPORCION_TRAIN)
    assert info_train["timestamp"].max() < info_test["timestamp"].min()


def test_escalador_ajustado_solo_con_train():
    X, info = datos()
    X_train, X_test, _, _ = dividir(X, info)
    X_train_esc, X_test_esc, escalador = escalar(X_train, X_test)
    np.testing.assert_allclose(escalador.mean_, X_train.mean().values)
    assert abs(X_train_esc["a"].mean()) < 1e-9
    assert X_test_esc["a"].mean() > 1      # el test, más reciente, queda por encima


def test_al_entrenar_solo_se_conocen_los_fraudes_ya_confirmados():
    from pipeline.preparacion_datos_ml import etiquetas_conocidas
    ts = pd.date_range("2026-03-01", periods=4, freq="D")
    info = pd.DataFrame({
        "timestamp": ts,
        "es_fraude": [1, 1, 1, 0],
        "tipo_fraude": ["bot", "bot", "bot", None],
        # confirmado a tiempo / después del final del train / nunca / legítima
        "fecha_confirmacion": [ts[1], ts[3] + pd.Timedelta(days=5), pd.NaT, pd.NaT],
    })
    info = etiquetas_conocidas(info)
    assert info["es_fraude"].tolist() == [1, 0, 0, 0]
    assert info["es_fraude_real"].tolist() == [1, 1, 1, 0]
    assert info["tipo_fraude"].iloc[0] == "bot"
    assert info["tipo_fraude"].iloc[1:].isna().all()
