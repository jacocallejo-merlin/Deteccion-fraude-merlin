import clickhouse_connect
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import HOST, PORT, USER, PASSWORD, DATABASE

SQL_FEATURES = '''
CREATE TABLE features_transaccion
ENGINE = MergeTree()
ORDER BY (cliente_id, timestamp)
AS
WITH
-- eventos de cada sesión con el tiempo desde el evento anterior (para detectar bots)
eventos AS (
    SELECT sesion_id, tipo, timestamp,
           dateDiff('second',
                    lagInFrame(timestamp) OVER (PARTITION BY sesion_id ORDER BY timestamp
                                                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
                    timestamp) AS seg_desde_evento_anterior,
           row_number() OVER (PARTITION BY sesion_id ORDER BY timestamp) AS n_evento
    FROM evento
),
-- resumen de la sesión HASTA el momento del pago (no se cuentan eventos posteriores)
sesion_hasta_pago AS (
    SELECT t.transaccion_id AS transaccion_id,
           countIf(e.timestamp <= t.timestamp) AS n_eventos_sesion,
           toUInt8(countIf(e.tipo = 'cambio_dato' AND e.timestamp <= t.timestamp) > 0) AS cambio_dato_sesion,
           -- minIfOrNull: con un solo evento antes del pago no hay intervalo -> NULL (luego 3600).
           -- minIf devolvería 0 y la sesión parecería un bot.
           minIfOrNull(e.seg_desde_evento_anterior, e.n_evento > 1 AND e.timestamp <= t.timestamp)
               AS min_seg_entre_eventos
    FROM transaccion AS t
    INNER JOIN eventos AS e ON t.sesion_id = e.sesion_id
    GROUP BY t.transaccion_id
),
-- devoluciones del cliente en los 30 días ANTERIORES a cada transacción
devoluciones_cliente AS (
    SELECT mp.cliente_id AS cliente_id, d.fecha AS fecha
    FROM devolucion AS d
    INNER JOIN transaccion AS t ON d.transaccion_id = t.transaccion_id
    INNER JOIN metodo_pago AS mp ON t.metodo_id = mp.metodo_id
),
devoluciones_previas AS (
    SELECT te.transaccion_id AS transaccion_id,
           countIf(dc.fecha < te.timestamp AND dc.fecha >= te.timestamp - INTERVAL 30 DAY)
               AS n_devoluciones_30d
    FROM transacciones_enriquecidas AS te
    INNER JOIN devoluciones_cliente AS dc ON te.cliente_id = dc.cliente_id
    GROUP BY te.transaccion_id
),
-- variables de ventana: velocidad de la tarjeta e historial del cliente
ventanas AS (
    SELECT
        transaccion_id,
        -- velocidad de la tarjeta (card testing, bots, smurfing)
        count() OVER tarjeta_10min                                   AS n_tarjeta_10min,
        count() OVER tarjeta_1h                                      AS n_tarjeta_1h,
        sum(toFloat64(cantidad)) OVER tarjeta_10min                  AS importe_tarjeta_10min,
        countIf(estado = 'rechazada') OVER tarjeta_1h                AS n_rechazadas_tarjeta_1h,
        count() OVER tarjeta_24h                                     AS n_tarjeta_24h,
        row_number() OVER tarjeta_orden                              AS n_orden_tarjeta,
        uniqExact(metodo_id) OVER cliente_24h                        AS n_tarjetas_cliente_24h,
        dateDiff('second', lagInFrame(timestamp) OVER tarjeta_orden_hasta_actual, timestamp)
                                                                     AS seg_desde_anterior_bruto,
        -- historial del cliente (pico de gasto): solo transacciones ANTERIORES
        count() OVER cliente_previas                                 AS n_previas_cliente,
        avg(log1p(toFloat64(cantidad))) OVER cliente_previas         AS media_log_importe_cliente,
        stddevSamp(log1p(toFloat64(cantidad))) OVER cliente_previas  AS std_log_importe_cliente,
        -- velocidad del CLIENTE (con cualquiera de sus tarjetas): transacciones en la hora
        -- anterior y tiempo desde su transacción anterior
        count() OVER cliente_1h                                      AS n_cliente_1h,
        dateDiff('second', lagInFrame(timestamp) OVER cliente_orden_hasta_actual, timestamp)
                                                                     AS seg_desde_anterior_cliente_bruto,
        -- dispositivo compartido: clientes distintos que lo han usado hasta ahora (incluida esta)
        uniqExact(cliente_id) OVER dispositivo_historial             AS n_clientes_dispositivo_bruto
    FROM transacciones_enriquecidas
    WINDOW
        tarjeta_10min AS (PARTITION BY metodo_id ORDER BY timestamp RANGE BETWEEN 600 PRECEDING AND 1 PRECEDING),
        tarjeta_1h    AS (PARTITION BY metodo_id ORDER BY timestamp RANGE BETWEEN 3600 PRECEDING AND 1 PRECEDING),
        tarjeta_24h   AS (PARTITION BY metodo_id ORDER BY timestamp RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING),
        tarjeta_orden AS (PARTITION BY metodo_id ORDER BY timestamp),
        tarjeta_orden_hasta_actual AS (PARTITION BY metodo_id ORDER BY timestamp
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        cliente_previas AS (PARTITION BY cliente_id ORDER BY timestamp
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING),
        cliente_orden_hasta_actual AS (PARTITION BY cliente_id ORDER BY timestamp
                                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        -- hora ANTERIOR sin contar la transacción actual (igual que tarjeta_1h)
        cliente_1h    AS (PARTITION BY cliente_id ORDER BY timestamp RANGE BETWEEN 3600 PRECEDING AND 1 PRECEDING),
        -- por CLIENTE (no por tarjeta): 24 h anteriores INCLUIDA la transacción actual,
        -- para que pagar con una 2.ª tarjeta distinta ya cuente 2
        cliente_24h   AS (PARTITION BY cliente_id ORDER BY timestamp RANGE BETWEEN 86400 PRECEDING AND CURRENT ROW),
        dispositivo_historial AS (PARTITION BY dispositivo_id ORDER BY timestamp
                                  ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT
    -- identificadores y etiqueta (la etiqueta NUNCA se usa como variable)
    te.transaccion_id AS transaccion_id, te.timestamp AS timestamp,
    assumeNotNull(te.cliente_id) AS cliente_id, te.metodo_id AS metodo_id,
    te.es_fraude AS es_fraude, te.tipo_fraude AS tipo_fraude,

    -- IMPORTE: tipo Decimal -> Float64 y logaritmo por la asimetría
    toFloat64(te.cantidad)                                    AS importe,
    log1p(toFloat64(te.cantidad))                             AS log_importe,
    if(te.limite_credito IS NULL, 0, toFloat64(te.cantidad) / toFloat64(te.limite_credito))  AS pct_limite_credito,

    -- FECHAS -> variables de tiempo
    toHour(te.timestamp)                                      AS hora,
    toDayOfWeek(te.timestamp)                                 AS dia_semana,
    toUInt8(toDayOfWeek(te.timestamp) >= 6)                   AS es_finde,
    toUInt8(toHour(te.timestamp) < 6)                         AS es_madrugada,
    dateDiff('day', te.fecha_alta, te.timestamp)              AS dias_desde_alta,

    -- CATEGÓRICAS -> indicadores 0/1
    -- (CAST a UInt8: las comparaciones sobre columnas LowCardinality heredarían ese tipo)
    -- Se descartan a propósito: moneda y tipo_operacion (siempre 'EUR' y 'compra': constantes),
    -- y categoria_negocio, comercio_pais, dispositivo_tipo y sistema_operativo (en el dataset no
    -- dependen del fraude; en one-hot añadirían ~30 columnas de ruido que diluyen las distancias
    -- de KMeans, OCSVM, DBSCAN y el autoencoder)
    toUInt8(te.sesion_id IS NOT NULL)                         AS es_online,
    CAST(ifNull(te.canal_tipo = 'app', 0), 'UInt8')                            AS canal_app,
    CAST(ifNull(te.canal_tipo = 'web', 0), 'UInt8')                            AS canal_web,
    CAST(ifNull(te.metodo_tipo = 'credito', 0), 'UInt8')                       AS es_credito,
    CAST(ifNull(te.estado = 'rechazada', 0), 'UInt8')                          AS rechazada,
    CAST(ifNull(te.metodo_autenticacion = 'ninguno', 0), 'UInt8')              AS sin_autenticacion,

    -- SESIÓN: los NULL de los pagos presenciales pasan a 0 (es_online ya indica que no hay sesión)
    toUInt8(ifNull(te.proxy_vpn, false))                      AS proxy_vpn,
    ifNull(te.num_intentos_login, 0)                          AS num_intentos_login,
    CAST(ifNull(te.ip_pais != te.cliente_pais, 0), 'UInt8') AS ip_extranjera,
    toUInt8(te.dispositivo_id IS NOT NULL AND cd.dispositivo_id IS NULL) AS dispositivo_nuevo,
    -- pagos presenciales (sin dispositivo) -> 0
    if(te.dispositivo_id IS NULL, 0, v.n_clientes_dispositivo_bruto) AS n_clientes_dispositivo,
    ifNull(s.n_eventos_sesion, 0)                             AS n_eventos_sesion,
    ifNull(s.cambio_dato_sesion, 0)                           AS cambio_dato_sesion,
    ifNull(s.min_seg_entre_eventos, 3600)                     AS min_seg_entre_eventos,

    -- VELOCIDAD DE LA TARJETA (transacciones rápidas repetitivas)
    v.n_tarjeta_10min                                         AS n_tarjeta_10min,
    v.n_tarjeta_1h                                            AS n_tarjeta_1h,
    ifNull(v.importe_tarjeta_10min, 0)                        AS importe_tarjeta_10min,
    v.n_rechazadas_tarjeta_1h                                 AS n_rechazadas_tarjeta_1h,
    v.n_tarjeta_24h                                           AS n_tarjeta_24h,
    if(v.n_orden_tarjeta = 1, 2592000, v.seg_desde_anterior_bruto) AS seg_desde_anterior_tarjeta,
    v.n_tarjetas_cliente_24h                                  AS n_tarjetas_cliente_24h,

    -- VELOCIDAD DEL CLIENTE (todas sus tarjetas). Su 1.ª transacción -> 30 días, como en la tarjeta
    v.n_cliente_1h                                            AS n_cliente_1h,
    if(v.n_previas_cliente = 0, 2592000, v.seg_desde_anterior_cliente_bruto) AS seg_desde_anterior_cliente,

    -- HISTORIAL DEL CLIENTE (picos de gasto): z-score del importe respecto a SU historial
    v.n_previas_cliente                                       AS n_previas_cliente,
    -- con menos de 5 compras previas no hay historial fiable -> 0 (neutro).
    -- greatest(std, 0.25): si el historial es casi constante, la std sería ~0 y el z-score explotaría
    if(v.n_previas_cliente >= 5,
       (log1p(toFloat64(te.cantidad)) - v.media_log_importe_cliente) / greatest(v.std_log_importe_cliente, 0.25),
       0)                                                     AS z_importe_cliente,
    if(v.n_previas_cliente >= 5,
       -- exp(x) - 1 deshace el log1p de la media: importe / importe típico (media geométrica)
       toFloat64(te.cantidad) / greatest(exp(v.media_log_importe_cliente) - 1, 0.01),
       1)                                                     AS ratio_importe_habitual,
    ifNull(dp.n_devoluciones_30d, 0)                          AS n_devoluciones_30d

FROM transacciones_enriquecidas AS te
LEFT JOIN cliente_dispositivo AS cd
       ON te.cliente_id = cd.cliente_id AND te.dispositivo_id = cd.dispositivo_id
LEFT JOIN sesion_hasta_pago    AS s  ON te.transaccion_id = s.transaccion_id
LEFT JOIN devoluciones_previas AS dp ON te.transaccion_id = dp.transaccion_id
LEFT JOIN ventanas             AS v  ON te.transaccion_id = v.transaccion_id
SETTINGS join_use_nulls = 1
'''

SENALES = {
    'es_madrugada = 1': 'Transacción de madrugada (0-6 h)',
    'ip_extranjera = 1': 'IP de otro país',
    'proxy_vpn = 1': 'Sesión con VPN',
    'dispositivo_nuevo = 1': 'Dispositivo no registrado del cliente',
    'n_clientes_dispositivo >= 3': 'Dispositivo usado por >= 3 clientes distintos',
    'n_tarjeta_10min >= 3': '>= 3 pagos de la tarjeta en 10 min antes',
    'min_seg_entre_eventos <= 3': 'Eventos separados <= 3 s (bot)',
    'z_importe_cliente > 3': 'Importe > 3 desviaciones de su historial',
    'pct_limite_credito >= 0.85': 'Importe >= 85 % del límite',
    'n_devoluciones_30d >= 2': '>= 2 devoluciones en los 30 días previos',
    'cambio_dato_sesion = 1': 'Cambio de datos en la sesión',
    'n_tarjeta_1h >= 3': '>= 3 pagos de la tarjeta en 1 h antes',
    'n_tarjetas_cliente_24h >= 2': '>= 2 tarjetas distintas del cliente en 24 h',
    'n_cliente_1h >= 3': '>= 3 pagos del cliente en 1 h antes',
    'seg_desde_anterior_cliente <= 60': '<= 60 s desde el pago anterior del cliente',
}


def main():
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    client.command('DROP TABLE IF EXISTS features_transaccion')
    client.command(SQL_FEATURES)

    n_trans = client.query('SELECT count() FROM transaccion').result_rows[0][0]
    n_feat = client.query('SELECT count() FROM features_transaccion').result_rows[0][0]
    tasa_global = client.query('SELECT avg(es_fraude) * 100 FROM features_transaccion').result_rows[0][0]

    print('Tabla "features_transaccion" creada.')
    print(f'  Filas: {n_feat:,} (transaccion: {n_trans:,}) {"OK" if n_feat == n_trans else "ERROR: no coinciden"}')
    print(f'  Tasa de fraude global: {tasa_global:.2f} %\n')
    print(f'  {"Señal":<45} {"Transacc.":>10} {"% fraude":>9} {"x media":>8}')
    for condicion, nombre in SENALES.items():
        n, tasa = client.query(
            f'SELECT count(), avg(es_fraude) * 100 FROM features_transaccion WHERE {condicion}'
        ).result_rows[0]
        veces = tasa / tasa_global if n else 0
        print(f'  {nombre:<45} {n:>10,} {tasa:>8.2f}% {veces:>7.1f}x')


if __name__ == '__main__':
    main()