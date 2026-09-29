import os
import clickhouse_connect
 
HOST = 'localhost'
PORT = 8123
USER = 'default'
PASSWORD = "password"
DATABASE = 'fraude_pagos'

# No usamos ni es_fraude ni tipo_fraude. La etiqueta solo se usa al final para evaluar.
# (patron_id, nombre, tipo_fraude al que apunta, nota_riesgo, condición SQL)
REGLAS = [
    (1, 'pico_gasto', 'pico_gasto', 0.60,'z_importe_cliente > 3 AND ratio_importe_habitual >= 4'),
    (2, 'card_testing', 'card_testing', 0.80,'n_tarjeta_10min >= 2 AND importe < 5'),
    (3, 'smurfing', 'smurfing', 0.75,'importe >= 850 AND importe < 1000 AND n_previos_umbral_24h >= 1'),
    (4, 'account_takeover', 'account_takeover', 0.85,'cambio_dato_sesion = 1 AND (num_intentos_login >= 3 OR dispositivo_nuevo = 1 OR ip_extranjera = 1)'),
    (5, 'ip_sospechosa', 'ip_sospechosa', 0.60,'ip_extranjera = 1 AND proxy_vpn = 1'),
    (6, 'dispositivo_nuevo', 'dispositivo_nuevo', 0.70,'dispositivo_nuevo = 1 AND ratio_importe_habitual >= 2'),
    (7, 'devolucion_abusiva', 'devolucion_abusiva', 0.50,'n_devoluciones_30d >= 2'),
    (8, 'bot', 'bot', 0.70,'min_seg_entre_eventos <= 3 AND n_eventos_sesion >= 4'),
    (9, 'bust_out', 'bust_out', 0.90,'pct_limite_credito >= 0.85'),
]
 

# Detectar patrnes de smurfing, cuantas compras realizo la misma tarjeta en 24h
SQL_BASE = '''
    SELECT *,
           countIf(importe >= 850 AND importe < 1000) OVER (
               PARTITION BY metodo_id ORDER BY timestamp
               RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING) AS n_previos_umbral_24h
    FROM features_transaccion
'''
 
def generar_alertas(client):
    client.command("ALTER TABLE alerta DELETE WHERE origen = 'regla' SETTINGS mutations_sync = 2")
    siguiente_id = client.query('SELECT ifNull(max(alerta_id), 0) + 1 FROM alerta').result_rows[0][0]
 
    selects = [
        f"SELECT transaccion_id, {pid} AS patron_id, toFloat32({nota}) AS nota_riesgo, timestamp "
        f"FROM base WHERE {condicion}"
        for pid, _, _, nota, condicion in REGLAS
    ]
    client.command(f'''
        INSERT INTO alerta (alerta_id, transaccion_id, origen, patron_id, nota_riesgo, fecha, estado)
        WITH base AS ({SQL_BASE})
        SELECT toUInt32({siguiente_id} + row_number() OVER (ORDER BY fecha, transaccion_id, patron_id) - 1),
               transaccion_id, 'regla', patron_id, nota_riesgo, fecha, 'pendiente'
        FROM ({' UNION ALL '.join(selects)})
             AS alertas (transaccion_id, patron_id, nota_riesgo, fecha)
    ''')
 
 
def evaluar(client):
    total_fraude = client.query('SELECT countIf(es_fraude = 1) FROM transaccion').result_rows[0][0]
 
    print(f'\n{"Regla":<20}{"Alertas":>8}{"Fraude":>8}{"Precisión":>11}{"Recall":>9}{"Recall tipo":>13}')
    print('-' * 69)
    for pid, nombre, tipo, _, _ in REGLAS:
        n, vp, vp_tipo, n_tipo = client.query(f'''
            SELECT count(), countIf(t.es_fraude = 1), countIf(t.tipo_fraude = '{tipo}'),
                   (SELECT countIf(tipo_fraude = '{tipo}') FROM transaccion)
            FROM alerta a INNER JOIN transaccion t ON a.transaccion_id = t.transaccion_id
            WHERE a.origen = 'regla' AND a.patron_id = {pid}
        ''').result_rows[0]
        precision = vp / n * 100 if n else 0
        print(f'{nombre:<20}{n:>8,}{vp:>8,}{precision:>10.1f}%{vp / total_fraude * 100:>8.1f}%'
              f'{vp_tipo / n_tipo * 100 if n_tipo else 0:>12.1f}%')
 
    n, vp = client.query('''
        SELECT count(), countIf(t.es_fraude = 1)
        FROM (SELECT DISTINCT transaccion_id FROM alerta WHERE origen = 'regla') a
        INNER JOIN transaccion t ON a.transaccion_id = t.transaccion_id
    ''').result_rows[0]
    precision, recall = vp / n * 100, vp / total_fraude * 100
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0
    print('-' * 69)
    print(f'{"TODAS (>= 1 regla)":<20}{n:>8,}{vp:>8,}{precision:>10.1f}%{recall:>8.1f}%')
    print(f'\nF1 del conjunto de reglas: {f1:.1f}%')
    print(f'Fraude no detectado por ninguna regla: {total_fraude - vp} de {total_fraude} transacciones')
 
    print('\nRecall por tipo de fraude (con todas las reglas):')
    filas = client.query('''
        SELECT t.tipo_fraude, count() AS n,
               countIf(t.transaccion_id IN (SELECT transaccion_id FROM alerta WHERE origen = 'regla')) AS detectadas
        FROM transaccion t WHERE t.es_fraude = 1
        GROUP BY t.tipo_fraude ORDER BY detectadas / n DESC
    ''').result_rows
    for tipo, n, det in filas:
        print(f'  {tipo:<20}{det:>4} de {n:<4}{det / n * 100:>6.1f}%')
 
 
def main():
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    generar_alertas(client)
    n = client.query("SELECT count() FROM alerta WHERE origen = 'regla'").result_rows[0][0]
    print(f'Alertas generadas por reglas: {n:,}')
    evaluar(client)
 
 
if __name__ == '__main__':
    main()