import clickhouse_connect
import numpy as np
import pandas as pd

# 1. CONFIGURACIÓN Y CONEXIÓN A CLICKHOUSE
HOST = "localhost"
PORT = 8123
USER = "default"
PASSWORD = "password"
DATABASE = "fraude_pagos"


def obtener_cliente_clickhouse():
    return clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )

# 2. EXTRACCIÓN DE DATOS DE LA TABLA ENRIQUECIDA

def extraer_datos_base(client) -> pd.DataFrame:
    query = """
        SELECT
            transaccion_id,
            cliente_id,
            metodo_id,
            timestamp,
            cantidad,
            limite_credito,
            cliente_pais,
            ip_pais,
            proxy_vpn,
            num_intentos_login,
            canal_tipo,
            estado,
            es_fraude
        FROM transacciones_enriquecidas
        ORDER BY cliente_id, timestamp ASC
    """
    df = client.query_df(query)

    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["cantidad"] = df["cantidad"].astype(float)
    df["limite_credito"] = df["limite_credito"].astype(float)
    df["num_intentos_login"] = df["num_intentos_login"].fillna(1).astype(int)
    df["proxy_vpn"] = df["proxy_vpn"].fillna(False).astype(int)

    return df


# 3. PIPELINE DE FEATURE ENGINEERING
def construir_caracteristicas(df: pd.DataFrame) -> pd.DataFrame:
    df_feat = df.copy()

    df_feat = df_feat.sort_values(by=["cliente_id", "timestamp"]).reset_index(
        drop=True
    )

    # A. Diferencia de tiempo entre transacciones consecutivas del cliente
    df_feat["diff_tiempo_seg"] = (
        df_feat.groupby("cliente_id")["timestamp"].diff().dt.total_seconds()
    )
    # -1 representa la primera transacción histórica observada del cliente
    df_feat["diff_tiempo_seg"] = df_feat["diff_tiempo_seg"].fillna(-1)

    # B. Variables temporales y de velocidad (Transacciones en última hora)
    df_feat["hora_dia"] = df_feat["timestamp"].dt.hour

    # Cálculo vectorizado robusto: cuenta transacciones en ventana móvil de 3600s
    tx_1h = np.empty(len(df_feat), dtype=np.int32)
    for _, grp in df_feat.groupby("cliente_id", sort=False):
        # Convertimos timestamps a segundos UNIX
        ts = grp["timestamp"].astype("int64").to_numpy() // 10**9
        # Búsqueda binaria del límite izquierdo (t - 3600 seg)
        left_idx = np.searchsorted(ts, ts - 3600, side="left")
        # Número de eventos dentro de la ventana de 1 hora
        tx_1h[grp.index] = np.arange(len(ts)) - left_idx + 1

    df_feat["tx_ultimas_1h"] = tx_1h

    # C. Tarjetas distintas por cliente
    tarjetas_unicas = (
        df_feat.groupby("cliente_id")["metodo_id"]
        .nunique()
        .rename("tarjetas_distintas_cliente")
    )
    df_feat = df_feat.merge(tarjetas_unicas, on="cliente_id", how="left")

    # D. Z-Score del importe (cantidad) a nivel de cliente
    stats_cliente = df_feat.groupby("cliente_id")["cantidad"].agg(
        media_cantidad="mean",
        std_cantidad=lambda x: x.std(ddof=0) if len(x) > 1 else 0.0,
    )

    df_feat = df_feat.merge(stats_cliente, on="cliente_id", how="left")

    # Z-Score: (x - media) / std
    df_feat["zscore_cantidad_cliente"] = np.where(
        df_feat["std_cantidad"] > 0,
        (df_feat["cantidad"] - df_feat["media_cantidad"])
        / df_feat["std_cantidad"],
        0.0,
    )
    df_feat["zscore_cantidad_cliente"] = (
        df_feat["zscore_cantidad_cliente"]
        .replace([np.inf, -np.inf], 0.0)
        .round(4)
    )

    # E. Ratios contextuales y de riesgo de negocio
    df_feat["ratio_cantidad_limite"] = np.where(
        (df_feat["limite_credito"].notnull()) & (df_feat["limite_credito"] > 0),
        df_feat["cantidad"] / df_feat["limite_credito"],
        0.0,
    ).round(4)

    df_feat["ip_distinta_pais"] = (
        (df_feat["ip_pais"].notnull())
        & (df_feat["cliente_pais"].notnull())
        & (df_feat["ip_pais"] != df_feat["cliente_pais"])
    ).astype(int)

    return df_feat


# 4. INSPECCIÓN Y VALIDACIÓN DEL DATASET RESULTANTE

def generar_reporte_features(df_features: pd.DataFrame):
    columnas_ingenieria = [
        "transaccion_id",
        "cliente_id",
        "cantidad",
        "diff_tiempo_seg",
        "hora_dia",
        "tx_ultimas_1h",
        "tarjetas_distintas_cliente",
        "zscore_cantidad_cliente",
        "ratio_cantidad_limite",
        "ip_distinta_pais",
        "proxy_vpn",
        "num_intentos_login",
        "es_fraude",
    ]

    df_subset = df_features[columnas_ingenieria]

    print("\n" + "=" * 80)
    print("RESUMEN GENERAL DEL CONJUNTO DE CARACTERÍSTICAS (HITO 3)")
    print("=" * 80)
    print(f"Total de registros procesados: {len(df_subset):,}")
    print(f"Total de características generadas: {len(columnas_ingenieria)}")

    print("\nVista preliminar de las primeras 5 filas")
    print(df_subset.head().to_string(index=False))

    print("\nEstadísticas descriptivas de las nuevas variables ")
    stats_cols = [
        "diff_tiempo_seg",
        "tx_ultimas_1h",
        "tarjetas_distintas_cliente",
        "zscore_cantidad_cliente",
        "ratio_cantidad_limite",
    ]
    print(df_subset[stats_cols].describe().round(2).to_string())

    print("\nComprobación de valores nulos")
    nulos = df_subset.isnull().sum()
    print(nulos[nulos > 0] if nulos.sum() > 0 else "0 valores nulos detectados.")
    print("=" * 80 + "\n")


# 5. EJECUCIÓN PRINCIPAL
if __name__ == "__main__":
    client = obtener_cliente_clickhouse()
    print(f"Conectado a ClickHouse en '{DATABASE}'. Extrayendo datos...")

    df_base = extraer_datos_base(client)
    print(f"Datos base obtenidos: {len(df_base):,} filas.")

    print("Ejecutando ingeniería de características...")
    df_resultado = construir_caracteristicas(df_base)

    generar_reporte_features(df_resultado)

    archivo_salida = "dataset_features_hito3.parquet"
    df_resultado.to_parquet(archivo_salida, index=False)
    print(f"Matriz de características guardada con éxito en '{archivo_salida}'.")