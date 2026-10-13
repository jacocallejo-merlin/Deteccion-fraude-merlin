"""Las variables de velocidad solo pueden contar transacciones ANTERIORES a cada una.
Se recalculan en pandas desde transacciones_enriquecidas y se comparan con features_transaccion.
Necesita ClickHouse con el pipeline ejecutado; si no, se salta."""
import numpy as np
import pandas as pd

# variable -> (columna por la que se agrupa, segundos de la ventana)
VENTANAS = {
    "n_tarjeta_10min": ("metodo_id", 600),
    "n_tarjeta_1h": ("metodo_id", 3600),
    "n_tarjeta_24h": ("metodo_id", 86400),
    "n_cliente_1h": ("cliente_id", 3600),
}


def contar_previas(df, grupo, segundos):
    """Transacciones del mismo grupo en [t - segundos, t - 1 s]: nunca la propia ni posteriores."""
    resultado = pd.Series(0, index=df.index)
    for _, g in df.groupby(grupo):
        t = g["t"].to_numpy()
        orden = np.argsort(t, kind="stable")
        ts = t[orden]
        hasta = np.searchsorted(ts, t, side="left")              # estrictamente antes de t
        desde = np.searchsorted(ts, t - segundos, side="left")   # desde t - segundos
        resultado.loc[g.index] = hasta - desde
    return resultado


def test_ventanas_solo_miran_al_pasado(clickhouse):
    te = clickhouse.query_df("SELECT transaccion_id, metodo_id, cliente_id, timestamp "
                             "FROM transacciones_enriquecidas")
    ft = clickhouse.query_df(f"SELECT transaccion_id, {', '.join(VENTANAS)} FROM features_transaccion")
    ts = pd.to_datetime(te["timestamp"])
    te["t"] = (ts - ts.min()).dt.total_seconds().astype("int64")
    df = te.merge(ft, on="transaccion_id", validate="one_to_one")
    assert len(df) == len(te)
    for variable, (grupo, segundos) in VENTANAS.items():
        esperado = contar_previas(df, grupo, segundos)
        distintas = (df[variable].astype(int) != esperado).sum()
        assert distintas == 0, f"{variable}: {distintas} transacciones no coinciden"


def test_una_fila_por_transaccion_tras_limpieza(clickhouse):
    n_trans, n_unicas = clickhouse.query(
        "SELECT count(), uniqExact(transaccion_id) FROM transaccion").result_rows[0]
    n_feat = clickhouse.command("SELECT count() FROM features_transaccion")
    assert n_trans == n_unicas == n_feat


def test_paises_normalizados(clickhouse):
    # sin limpieza, " España" != "España" marcaría como extranjera una IP del propio país
    n = clickhouse.command("SELECT count() FROM transacciones_enriquecidas "
                           "WHERE ip_pais IS NOT NULL AND ip_pais != cliente_pais "
                           "AND lowerUTF8(trimBoth(ip_pais)) = lowerUTF8(trimBoth(cliente_pais))")
    assert n == 0
