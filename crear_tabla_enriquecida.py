import os
import clickhouse_connect

HOST = 'localhost'
PORT = 8123
USER = 'default'
PASSWORD = 'password'
DATABASE = 'fraude_pagos'

SQL_CREAR = '''
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

def main():
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    client.command('DROP TABLE IF EXISTS transacciones_enriquecidas')
    client.command(SQL_CREAR)

    n_trans = client.query('SELECT count() FROM transaccion').result_rows[0][0]
    n_enriq = client.query('SELECT count() FROM transacciones_enriquecidas').result_rows[0][0]
    sin_cliente = client.query(
        'SELECT countIf(cliente_id IS NULL) FROM transacciones_enriquecidas').result_rows[0][0]
    presenciales = client.query(
        "SELECT countIf(canal_tipo = 'datafono'), countIf(canal_tipo = 'datafono' AND ip_pais IS NULL) "
        'FROM transacciones_enriquecidas').result_rows[0]

    print('Tabla "transacciones_enriquecidas" creada.')
    print(f'  Filas en transaccion:                 {n_trans:,}')
    print(f'  Filas en transacciones_enriquecidas:  {n_enriq:,}  '
          f'{"OK" if n_trans == n_enriq else "ERROR: no coinciden"}')
    print(f'  Transacciones sin cliente (debe ser 0): {sin_cliente}')
    print(f'  Presenciales: {presenciales[0]:,} (con sesión a NULL: {presenciales[1]:,})')


if __name__ == '__main__':
    main()
