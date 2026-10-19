"""
Una sola cola de alertas y los avisos de los casos graves (Hito 6, la web).

1. Vista alertas_unificadas: junta la nota de riesgo del modelo que ordena la cola
   (riesgo_transaccion, la escribe simular_analistas.py) con las reglas que saltaron (alerta) y le
   da a cada transacción un nivel: crítico, alto o (si se activa) medio (config.yaml, sección
   alertas). Lo que no llega al nivel más bajo no es una alerta.
2. Casos: las alertas de una misma tarjeta en el mismo día van juntas (card testing, cash-out en
   varios comercios, bots... son varias transacciones seguidas).
3. Avisos: por cada caso nuevo de nivel alertas.avisar_desde o más, un aviso en la tabla aviso (los
   ve la web) y, si hay alertas.webhook_url, se envían también a ese sistema.

Se puede repetir sin duplicar: cada caso tiene un id fijo (tarjeta + día) y solo se avisa de los
nuevos. Con --rehacer se reescriben también los ya creados (p. ej. tras cambiar la sección alertas
de config.yaml), sin volver a enviarlos al webhook.
"""
import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import clickhouse_connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG, DATABASE, HOST, PASSWORD, PORT, USER

_cfg = CONFIG['alertas']
NIVELES = {'medio': 1, 'alto': 2, 'critico': 3}
NOMBRE_NIVEL = {3: 'crítico', 2: 'alto', 1: 'medio'}
# El nivel medio es opcional (alertas.medio: null = no se usa): son alertas que no piden ninguna acción
CONDICION_MEDIO = f"pct_dia <= {_cfg['medio']} OR notEmpty(reglas)" if _cfg.get('medio') else '0'

SQL_VISTA = f'''
CREATE OR REPLACE VIEW alertas_unificadas AS
SELECT r.transaccion_id AS transaccion_id, r.dia AS dia, r.score AS score, r.posicion AS posicion,
       r.transacciones_dia AS transacciones_dia, r.posicion / r.transacciones_dia AS pct_dia,
       r.modelo AS modelo, r.en_cola AS en_cola, g.reglas AS reglas, g.nota_regla AS nota_regla,
       multiIf(pct_dia <= {_cfg['critico']}, 3,
               pct_dia <= {_cfg['alto']} OR nota_regla >= {_cfg['nota_regla_alto']}, 2,
               {CONDICION_MEDIO}, 1, 0) AS prioridad
FROM riesgo_transaccion AS r FINAL
LEFT JOIN (SELECT a.transaccion_id AS transaccion_id, groupArray(p.nombre) AS reglas,
                  max(a.nota_riesgo) AS nota_regla
           FROM alerta AS a INNER JOIN patron AS p ON a.patron_id = p.patron_id
           WHERE a.origen = 'regla' GROUP BY a.transaccion_id) AS g
       ON g.transaccion_id = r.transaccion_id
WHERE prioridad > 0
'''

SQL_CASOS = '''
SELECT cityHash64(te.metodo_id, a.dia) AS aviso_id, a.dia AS dia, te.metodo_id AS metodo_id,
       any(assumeNotNull(te.cliente_id)) AS cliente_id, any(mp.numero_enmascarado) AS tarjeta,
       max(a.prioridad) AS prioridad, count() AS transacciones, toFloat64(sum(te.cantidad)) AS importe,
       groupUniqArray(assumeNotNull(te.comercio_id)) AS comercios,
       groupUniqArrayArray(a.reglas) AS reglas, groupArray(a.transaccion_id) AS ids,
       min(te.timestamp) AS desde, max(te.timestamp) AS hasta
FROM alertas_unificadas AS a
INNER JOIN transacciones_enriquecidas AS te ON a.transaccion_id = te.transaccion_id
LEFT JOIN metodo_pago AS mp ON te.metodo_id = mp.metodo_id
GROUP BY te.metodo_id, a.dia
HAVING prioridad >= {minimo:UInt8}
'''


# Señales en lenguaje claro a partir de las variables de una transacción (también las usa la web)
VARIABLES = ['ip_pais', 'dispositivo_nuevo', 'ip_extranjera', 'proxy_vpn', 'n_tarjeta_10min', 'n_tarjeta_1h',
             'n_comercios_cliente_3h', 'ratio_importe_habitual', 'pct_limite_credito', 'min_seg_entre_eventos',
             'num_intentos_login', 'cambio_dato_sesion', 'n_devoluciones_30d', 'es_madrugada', 'rechazada']
MOTIVO_MODELO = 'Patrón poco habitual para este cliente (según el modelo)'


def senales(v):
    s = []
    if v['dispositivo_nuevo']:
        s.append('Dispositivo que el cliente no había usado nunca')
    if v['ip_extranjera']:
        s.append(f"Conexión desde otro país ({v['ip_pais'] or 'desconocido'})")
    if v['proxy_vpn']:
        s.append('Conexión a través de proxy o VPN')
    if v['n_tarjeta_10min'] >= 2:
        s.append(f"{v['n_tarjeta_10min']} pagos con la misma tarjeta en los 10 minutos anteriores")
    elif v['n_tarjeta_1h'] >= 3:
        s.append(f"{v['n_tarjeta_1h']} pagos con la misma tarjeta en la hora anterior")
    if v['n_comercios_cliente_3h'] >= 2:
        s.append(f"Compras en {v['n_comercios_cliente_3h'] + 1} comercios distintos en 3 horas")
    if v['ratio_importe_habitual'] >= 3:
        s.append(f"Importe {v['ratio_importe_habitual']:.0f} veces el habitual del cliente")
    if v['pct_limite_credito'] >= 0.8:
        s.append(f"Usa el {v['pct_limite_credito'] * 100:.0f} % del límite de la tarjeta")
    if v['min_seg_entre_eventos'] <= 3:
        s.append('Acciones en la web o app a velocidad no humana (posible bot)')
    if v['num_intentos_login'] >= 3:
        s.append(f"{v['num_intentos_login']} intentos de inicio de sesión")
    if v['cambio_dato_sesion']:
        s.append('Cambio de datos de la cuenta en la misma sesión')
    if v['n_devoluciones_30d'] >= 2:
        s.append(f"{v['n_devoluciones_30d']} devoluciones en los últimos 30 días")
    if v['es_madrugada']:
        s.append('De madrugada')
    if v['rechazada']:
        s.append('El pago fue rechazado')
    return s


def conectar():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                         database=DATABASE)


def crear_tablas(client):
    client.command('''
        CREATE TABLE IF NOT EXISTS aviso (
            aviso_id       UInt64,                   -- tarjeta + día: un aviso por caso
            fecha          DateTime,                 -- cuándo salta (al cerrar el día, con el scoring)
            dia            Date,
            metodo_id      UInt32,
            cliente_id     UInt32,
            nivel          LowCardinality(String),   -- critico, alto o medio
            transacciones  UInt32,
            importe        Float64,
            comercios      Array(UInt32),
            mensaje        String,
            creado         DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(creado)
        ORDER BY aviso_id
    ''')
    # Avisos que alguien ha marcado como leídos en la web (solo se añaden filas)
    client.command('''
        CREATE TABLE IF NOT EXISTS aviso_leido (
            aviso_id  UInt64,
            comercio  UInt32,                        -- 0 = vista de la entidad (todos los comercios)
            leido     DateTime DEFAULT now()
        ) ENGINE = MergeTree
        ORDER BY (aviso_id, comercio)
    ''')


def euros(x):
    return f'{x:,.2f} €'.replace(',', 'X').replace('.', ',').replace('X', '.')


def ultimos_digitos(tarjeta):
    """'**** **** **** 5926' -> '•••• 5926'."""
    return f"•••• {str(tarjeta or '')[-4:]}"


def mensaje(caso, nombres_comercio):
    n = caso['transacciones']
    comercios = [nombres_comercio.get(c, f'comercio {c}') for c in caso['comercios']]
    donde = comercios[0] if len(comercios) == 1 else f'{len(comercios)} comercios ({", ".join(comercios[:3])}' \
        + ('…)' if len(comercios) > 3 else ')')
    motivos = list(caso['reglas']) + [s for s in caso['senales'] if s not in caso['reglas']]
    motivo = '; '.join(m[0].lower() + m[1:] for m in (motivos[:3] or [MOTIVO_MODELO]))
    return (f'Tarjeta {ultimos_digitos(caso["tarjeta"])}: {n} pago{"s" if n != 1 else ""} '
            f'sospechoso{"s" if n != 1 else ""} por {euros(caso["importe"])} en {donde.rstrip(".")}. '
            f'Motivo: {motivo}.')


def enviar_webhook(url, avisos):
    """Envía los avisos nuevos a otro sistema. Si falla, se avisa pero no se para el pipeline."""
    texto = '\n'.join(f'[{a["nivel"].upper()}] {a["mensaje"]}' for a in avisos[:50])
    cuerpo = json.dumps({'text': f'{len(avisos)} aviso/s de fraude nuevos:\n{texto}', 'avisos': avisos},
                        default=str).encode('utf-8')
    peticion = urllib.request.Request(url, data=cuerpo, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(peticion, timeout=10) as r:
            print(f'Webhook: {len(avisos)} aviso/s enviados ({r.status})')
    except Exception as e:
        print(f'Webhook: no se han podido enviar los avisos ({e}). Quedan en la web.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rehacer', action='store_true', help='reescribe también los avisos ya creados')
    args = parser.parse_args()
    client = conectar()
    if not client.command('EXISTS TABLE riesgo_transaccion'):
        raise SystemExit('No existe riesgo_transaccion: lanza antes simular_analistas.py')
    client.command(SQL_VISTA)
    crear_tablas(client)

    conteo = dict(client.query('SELECT prioridad, count() FROM alertas_unificadas GROUP BY prioridad').result_rows)
    print(f'Cola de alertas (modelo + reglas): {sum(conteo.values()):,} transacciones -> '
          + ', '.join(f'{conteo.get(p, 0):,} de nivel {NOMBRE_NIVEL[p]}' for p in (3, 2, 1)))

    minimo = NIVELES[_cfg['avisar_desde']]
    casos = list(client.query(SQL_CASOS, parameters={'minimo': minimo}).named_results())
    existentes = {i for (i,) in client.query('SELECT aviso_id FROM aviso').result_rows}
    nombres = dict(client.query('SELECT comercio_id, nombre FROM comercio').result_rows)
    if not args.rehacer:
        casos = [c for c in casos if c['aviso_id'] not in existentes]
    ids = [i for c in casos for i in c['ids']]
    variables = {}
    if ids:
        filas = client.query(f"""
            SELECT v.transaccion_id AS transaccion_id, te.ip_pais AS ip_pais,
                   {', '.join(f'v.{c} AS {c}' for c in VARIABLES[1:])}
            FROM features_transaccion AS v
            INNER JOIN transacciones_enriquecidas AS te ON te.transaccion_id = v.transaccion_id
            WHERE v.transaccion_id IN {{ids:Array(UInt32)}}""", parameters={'ids': ids}).named_results()
        variables = {f['transaccion_id']: f for f in filas}
    nuevos = []
    for caso in casos:
        caso['senales'] = list(dict.fromkeys(s for i in caso['ids'] if i in variables for s in senales(variables[i])))
        nivel = {p: n for n, p in NIVELES.items()}[caso['prioridad']]
        nuevos.append({
            'aviso_id': caso['aviso_id'], 'dia': caso['dia'],
            'fecha': datetime.combine(caso['dia'] + timedelta(days=1), datetime.min.time()),
            'metodo_id': caso['metodo_id'], 'cliente_id': caso['cliente_id'], 'nivel': nivel,
            'transacciones': caso['transacciones'], 'importe': caso['importe'],
            'comercios': list(caso['comercios']), 'mensaje': mensaje(caso, nombres),
        })
    if nuevos:
        columnas = list(nuevos[0])
        client.insert('aviso', [[a[c] for c in columnas] for a in nuevos], column_names=columnas)
    if args.rehacer:
        print(f'Reescritos {len(nuevos) - sum(a["aviso_id"] not in existentes for a in nuevos)} avisos ya existentes')
        nuevos = [a for a in nuevos if a['aviso_id'] not in existentes]
    criticos = sum(a['nivel'] == 'critico' for a in nuevos)
    print(f'Avisos nuevos (casos de nivel {_cfg["avisar_desde"]} o más): {len(nuevos)} ({criticos} críticos); '
          f'ya había {len(existentes)}')
    for a in sorted(nuevos, key=lambda a: (a['dia'], -NIVELES[a['nivel']]))[-5:]:
        print(f'  {a["dia"]} [{a["nivel"]}] {a["mensaje"]}')

    if nuevos and _cfg.get('webhook_url'):
        enviar_webhook(_cfg['webhook_url'], nuevos)


if __name__ == '__main__':
    main()
