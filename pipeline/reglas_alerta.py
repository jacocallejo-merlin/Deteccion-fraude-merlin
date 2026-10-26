import clickhouse_connect
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG, HOST, PORT, USER, PASSWORD, DATABASE

# No usamos ni es_fraude ni tipo_fraude. La etiqueta solo se usa al final para evaluar.
# Cada regla: (patron_id, nota_riesgo, condición SQL con sus umbrales entre llaves). El nombre de la
# regla es el tipo de fraude al que apunta.
# Tres reglas se rediseñaron para los tipos de fraude que los detectores casi no ven (pico de gasto,
# IP sospechosa y smurfing), con los fraudes conocidos del periodo de entrenamiento:
#  - pico_gasto: la mitad de estos fraudes es de madrugada (frente al 8 % de los pagos legítimos), así
#    que de madrugada basta con un pico algo menor (segunda condición).
#  - ip_sospechosa: todos llegan desde otro país, pero solo ~78 % con VPN; los demás suelen ser de
#    madrugada. Antes se exigía siempre la VPN.
#  - smurfing: el primer pago de cada serie no tiene otro igual en las 24 h anteriores y no saltaba;
#    se detecta también porque el importe es muy superior al gasto habitual del cliente.
# Criterio para adoptarlos: que al menos 1 de cada 5 alertas nuevas fuera fraude en ese periodo.
# Revisadas después (medir_sistema.py), también solo con el periodo de entrenamiento:
#  - card_testing: la regla original (>= 2 pagos de la tarjeta en 10 min) no veía el primer pago de cada
#    ráfaga, que aún no tiene pagos previos. Todos son de menos de 5 € y casi siempre llegan desde un
#    dispositivo nuevo o con VPN: así caza 50 de 53 (antes 39) con 0,9 falsas alarmas al día (antes 2,7).
#  - cash_out_multicomercio (nueva): el fraude que ningún modelo había visto (solo aparece en el examen).
#    No se puede ajustar con sus casos, así que el umbral se eligió con los pagos legítimos del
#    entrenamiento: >= 3 comercios distintos del cliente en 3 h e importe >= 1,5 veces el habitual da
#    0,2 falsas alarmas al día (con >= 3 comercios solos serían 1,2 al día).
PLANTILLAS = {
    'pico_gasto':         (1, 0.60, '(z_importe_cliente > {z_minimo} AND ratio_importe_habitual >= {ratio_minimo}) '
                                    'OR (es_madrugada = 1 AND z_importe_cliente > {z_madrugada_minimo} '
                                    'AND ratio_importe_habitual >= {ratio_madrugada_minimo})'),
    'card_testing':       (2, 0.80, 'importe < {importe_maximo} AND (dispositivo_nuevo = 1 OR proxy_vpn = 1 '
                                    'OR n_tarjeta_10min >= {pagos_10min_minimo})'),
    'smurfing':           (3, 0.75, 'importe >= {importe_minimo} AND importe < 1000 '
                                    'AND (n_previos_umbral_24h >= {previos_24h_minimo} '
                                    'OR ratio_importe_habitual >= {ratio_minimo})'),
    'account_takeover':   (4, 0.85, 'cambio_dato_sesion = 1 AND (num_intentos_login >= {intentos_login_minimo} '
                                    'OR dispositivo_nuevo = 1 OR ip_extranjera = 1)'),
    'ip_sospechosa':      (5, 0.60, 'ip_extranjera = 1 AND (proxy_vpn = 1 OR es_madrugada = 1)'),
    'dispositivo_nuevo':  (6, 0.70, 'dispositivo_nuevo = 1 AND ratio_importe_habitual >= {ratio_minimo}'),
    'devolucion_abusiva': (7, 0.50, 'n_devoluciones_30d >= {devoluciones_30d_minimo}'),
    'bot':                (8, 0.70, 'min_seg_entre_eventos <= {seg_entre_eventos_maximo} '
                                    'AND n_eventos_sesion >= {eventos_minimo}'),
    'bust_out':           (9, 0.90, 'pct_limite_credito >= {pct_limite_minimo}'),
    'cash_out_multicomercio': (10, 0.85, 'n_comercios_cliente_3h >= {comercios_minimo} '
                                         'AND ratio_importe_habitual >= {ratio_minimo}'),
}

# Patrones que se añadieron después de generar el dataset (dataset.py los crea en una base de datos nueva):
# en una que ya existe se dan de alta al generar las alertas
PATRONES_NUEVOS = {
    10: ('Cash-out multicomercio', 'Vaciar la tarjeta comprando en muchas tiendas en poco tiempo',
         '>= 3 comercios distintos del cliente en 3 h e importe >= 1,5 veces su habitual'),
}

# Umbrales con los que se diseñaron las reglas (Hito 3, a mano). Los que se usan están en
# config.yaml (sección reglas); ajustar_umbrales.py (Hito 6) los ajusta con lo validado y compara
# el resultado con estos.
UMBRALES_INICIALES = {
    'pico_gasto':         {'activa': True, 'z_minimo': 3, 'ratio_minimo': 4,
                           'z_madrugada_minimo': 2.5, 'ratio_madrugada_minimo': 4},
    'card_testing':       {'activa': True, 'pagos_10min_minimo': 2, 'importe_maximo': 5},
    'smurfing':           {'activa': True, 'importe_minimo': 850, 'previos_24h_minimo': 1, 'ratio_minimo': 10},
    'account_takeover':   {'activa': True, 'intentos_login_minimo': 3},
    'ip_sospechosa':      {'activa': True},
    'dispositivo_nuevo':  {'activa': True, 'ratio_minimo': 2},
    'devolucion_abusiva': {'activa': True, 'devoluciones_30d_minimo': 2},
    'bot':                {'activa': True, 'seg_entre_eventos_maximo': 3, 'eventos_minimo': 4},
    'bust_out':           {'activa': True, 'pct_limite_minimo': 0.85},
    'cash_out_multicomercio': {'activa': True, 'comercios_minimo': 3, 'ratio_minimo': 1.5},
}


def construir_reglas(umbrales):
    """(patron_id, nombre, tipo_fraude al que apunta, nota_riesgo, condición SQL) de las reglas activas."""
    return [(pid, nombre, nombre, nota, plantilla.format(**umbrales[nombre]))
            for nombre, (pid, nota, plantilla) in PLANTILLAS.items() if umbrales[nombre]['activa']]


UMBRALES = CONFIG['reglas']
REGLAS = construir_reglas(UMBRALES)


def sql_base(importe_minimo_smurfing):
    """features_transaccion + compras previas de la tarjeta en 24 h en la franja del smurfing
    (justo por debajo de 1.000 €), que usa la regla de smurfing."""
    return f'''
    SELECT *,
           countIf(importe >= {importe_minimo_smurfing} AND importe < 1000) OVER (
               PARTITION BY metodo_id ORDER BY timestamp
               RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING) AS n_previos_umbral_24h
    FROM features_transaccion
'''


SQL_BASE = sql_base(UMBRALES['smurfing']['importe_minimo'])


def asegurar_patrones(client):
    existentes = {p for (p,) in client.query('SELECT patron_id FROM patron').result_rows}
    faltan = [[pid, *textos] for pid, textos in PATRONES_NUEVOS.items() if pid not in existentes]
    if faltan:
        client.insert('patron', faltan, column_names=['patron_id', 'nombre', 'descripcion', 'condicion'])


def generar_alertas(client):
    asegurar_patrones(client)
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
        recall_regla = vp / total_fraude * 100 if total_fraude else 0
        print(f'{nombre:<20}{n:>8,}{vp:>8,}{precision:>10.1f}%{recall_regla:>8.1f}%'
            f'{vp_tipo / n_tipo * 100 if n_tipo else 0:>12.1f}%')
 
    n, vp = client.query('''
        SELECT count(), countIf(t.es_fraude = 1)
        FROM (SELECT DISTINCT transaccion_id FROM alerta WHERE origen = 'regla') a
        INNER JOIN transaccion t ON a.transaccion_id = t.transaccion_id
    ''').result_rows[0]
    precision = vp / n * 100 if n else 0
    recall = vp / total_fraude * 100 if total_fraude else 0
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
    desactivadas = [nombre for nombre, u in UMBRALES.items() if not u['activa']]
    if desactivadas:
        print(f'Reglas desactivadas en config.yaml: {", ".join(desactivadas)}')
    evaluar(client)
 
 
if __name__ == '__main__':
    main()