import clickhouse_connect
from config import HOST, PORT, USER, PASSWORD, DATABASE

SQL_TRANSACCIONES = '''
CREATE TABLE transacciones_enriquecidas
ENGINE = MergeTree()
ORDER BY (metodo_id, timestamp)
AS
SELECT
    -- transacción
    t.transaccion_id AS transaccion_id, t.timestamp AS timestamp, t.cantidad AS cantidad,
    t.moneda AS moneda, t.tipo_operacion AS tipo_operacion,
    t.metodo_autenticacion AS metodo_autenticacion, t.estado AS estado,
    -- etiqueta (solo para evaluar, NUNCA como feature de entrenamiento)
    t.es_fraude AS es_fraude, t.tipo_fraude AS tipo_fraude,
    -- método de pago
    t.metodo_id AS metodo_id, mp.tipo AS metodo_tipo, mp.entidad_emisora AS entidad_emisora,
    mp.limite_credito AS limite_credito,
    -- cliente (se llega a través del método de pago)
    c.cliente_id AS cliente_id, c.pais AS cliente_pais, c.fecha_alta AS fecha_alta,
    -- canal y comercio (se llega a través del canal)
    t.canal_id AS canal_id, cp.tipo AS canal_tipo,
    co.comercio_id AS comercio_id, co.nombre AS comercio_nombre,
    co.categoria_negocio AS categoria_negocio, co.pais AS comercio_pais,
    -- dispositivo (NULL si el pago es presencial)
    t.dispositivo_id AS dispositivo_id, d.tipo AS dispositivo_tipo,
    d.sistema_operativo AS sistema_operativo,
    -- sesión (NULL si el pago es presencial)
    t.sesion_id AS sesion_id, s.ip_pais AS ip_pais, s.proxy_vpn AS proxy_vpn,
    s.num_intentos_login AS num_intentos_login, s.resultado_login AS resultado_login
FROM transaccion AS t
LEFT JOIN metodo_pago AS mp ON t.metodo_id      = mp.metodo_id
LEFT JOIN cliente     AS c  ON mp.cliente_id    = c.cliente_id
LEFT JOIN canal_pago  AS cp ON t.canal_id       = cp.canal_id
LEFT JOIN comercio    AS co ON cp.comercio_id   = co.comercio_id
LEFT JOIN dispositivo AS d  ON t.dispositivo_id = d.dispositivo_id
LEFT JOIN sesion      AS s  ON t.sesion_id      = s.sesion_id
SETTINGS join_use_nulls = 1
'''

SQL_ALERTAS = '''
CREATE OR REPLACE VIEW alertas_enriquecidas AS
SELECT
    -- alerta
    a.alerta_id AS alerta_id, a.fecha AS fecha_alerta, a.origen AS origen,
    a.nota_riesgo AS nota_riesgo, a.estado AS estado,
    -- patrón que la disparó (NULL si la genera un modelo)
    a.patron_id AS patron_id, p.nombre AS patron_nombre,
    -- revisión humana (NULL mientras está pendiente)
    a.analista_id AS analista_id, an.nombre AS analista_nombre, an.nivel AS analista_nivel,
    a.fecha_revision AS fecha_revision,
    dateDiff('minute', a.fecha, a.fecha_revision) AS minutos_hasta_revision,
    a.veredicto AS veredicto, a.comentario AS comentario,
    -- transacción alertada (es_fraude solo para medir aciertos y falsos positivos)
    a.transaccion_id AS transaccion_id, t.timestamp AS fecha_transaccion,
    t.cantidad AS cantidad, t.es_fraude AS es_fraude, t.tipo_fraude AS tipo_fraude
FROM alerta AS a
LEFT JOIN transaccion AS t  ON a.transaccion_id = t.transaccion_id
LEFT JOIN patron      AS p  ON a.patron_id      = p.patron_id
LEFT JOIN analista    AS an ON a.analista_id    = an.analista_id
SETTINGS join_use_nulls = 1
'''


def main():
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )

    client.command('DROP TABLE IF EXISTS transacciones_enriquecidas')
    client.command(SQL_TRANSACCIONES)

    n_trans = client.command('SELECT count() FROM transaccion')
    n_enriq = client.command('SELECT count() FROM transacciones_enriquecidas')
    sin_cliente, sin_comercio, presenciales, presenciales_null, online_sin_sesion = client.query('''
        SELECT countIf(cliente_id IS NULL),
               countIf(comercio_id IS NULL),
               countIf(canal_tipo = 'datafono'),
               countIf(canal_tipo = 'datafono' AND sesion_id IS NULL AND ip_pais IS NULL),
               countIf(canal_tipo != 'datafono' AND (sesion_id IS NULL OR ip_pais IS NULL))
        FROM transacciones_enriquecidas
    ''').result_rows[0]

    print('Tabla "transacciones_enriquecidas" creada.')
    print(f'  Filas en transaccion:                  {n_trans:,}')
    print(f'  Filas en transacciones_enriquecidas:   {n_enriq:,}  '
          f'{"OK" if n_trans == n_enriq else "ERROR: no coinciden"}')
    print(f'  Sin cliente (debe ser 0):              {sin_cliente}')
    print(f'  Sin comercio (debe ser 0):             {sin_comercio}')
    print(f'  Presenciales: {presenciales:,} (con sesión a NULL: {presenciales_null:,})  '
          f'{"OK" if presenciales == presenciales_null else "REVISAR"}')
    print(f'  Online sin sesión (debe ser 0):        {online_sin_sesion}')

    client.command(SQL_ALERTAS)
    n_alertas = client.command('SELECT count() FROM alerta')
    n_vista = client.command('SELECT count() FROM alertas_enriquecidas')
    print('\nVista "alertas_enriquecidas" creada.')
    print(f'  Filas en alerta: {n_alertas:,} | en la vista: {n_vista:,}  '
          f'{"OK" if n_alertas == n_vista else "ERROR: no coinciden"}')
    if n_alertas == 0:
        print('  (alerta está vacía: se rellenará con las reglas y modelos de los siguientes pasos)')


if __name__ == '__main__':
    main()