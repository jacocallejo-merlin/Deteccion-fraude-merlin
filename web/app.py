"""
Web del sistema antifraude: la herramienta del equipo que revisa el fraude (entidad de pagos) o de
un comercio (eligiendo el comercio arriba).

    uvicorn web.app:app --reload        # desde la raíz del proyecto -> http://localhost:8000
                                        # documentación de la API: http://localhost:8000/api/docs

Lee de ClickHouse lo que deja el pipeline (alertas_unificadas, aviso, transacciones...) y solo
escribe dos cosas: los veredictos que da una persona (revision_web, que el recalibrado semanal usa
como feedback) y los avisos marcados como leídos (aviso_leido).

La detección es automática; nadie tiene que revisarlo todo. Cada alerta trae una acción recomendada
según su nivel y la probabilidad de que sea fraude (lo que han resultado ser los casos cerrados de
ese nivel), y se cierra sola: fraude si llega el contracargo del banco, "sin incidencia" si pasan
alertas.dias_cierre días sin él. Revisar a mano es opcional y adelanta el cierre.

"Ahora" es la hora de los datos, no la del ordenador: el final del último día puntuado. Así una
demo con datos de 2026 se comporta como si el sistema estuviera en marcha ese día.
"""
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import clickhouse_connect
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / 'pipeline'))
from config import CONFIG, DATABASE, HOST, PASSWORD, PORT, USER
from generar_avisos import MOTIVO_MODELO, VARIABLES, senales

ESTATICOS = Path(__file__).resolve().parent / 'static'
NIVEL = {3: 'critico', 2: 'alto', 1: 'medio'}
DIAS_CIERRE = CONFIG['alertas'].get('dias_cierre', 30)
NIVELES = ['critico', 'alto'] + (['medio'] if CONFIG['alertas'].get('medio') else [])   # el medio es opcional
ABIERTOS = ('pendiente', 'vigilancia')        # estados de una alerta sin cerrar

# Qué hacer con cada caso, según su nivel y quién lo mira (la entidad o el propio comercio)
ACCIONES = {
    'critico': {
        'comercio': ('Retener el pedido',
                     'No envíes el pedido ni prestes el servicio hasta que se aclare. Si ya está entregado, valora '
                     'devolver el importe antes de que llegue el contracargo: te ahorras la comisión.'),
        'entidad': ('Bloquear la tarjeta',
                    'Bloquea la tarjeta de forma preventiva y contacta con el titular para que confirme los pagos.'),
    },
    'alto': {
        'comercio': ('Confirmar con el cliente',
                     'Contacta con el cliente (teléfono o email de su cuenta) para que confirme la compra antes de '
                     'servir el pedido.'),
        'entidad': ('Revisar hoy',
                    'Revisa el caso hoy y, si hay dudas, contacta con el titular antes de bloquear la tarjeta.'),
    },
    'medio': {
        'comercio': ('Solo vigilar', f'No hace falta hacer nada: el sistema lo vigila y se cerrará solo si en '
                                     f'{DIAS_CIERRE} días no hay reclamación.'),
        'entidad': ('Solo vigilar', f'No hace falta revisarlo: se cierra solo si en {DIAS_CIERRE} días no hay '
                                    f'contracargo.'),
    },
}


def accion(nivel, comercio):
    corta, texto = ACCIONES[nivel]['comercio' if comercio else 'entidad']
    return {'corta': corta, 'texto': texto}

app = FastAPI(title='Centinela · API', docs_url='/api/docs', redoc_url=None)
app.mount('/static', StaticFiles(directory=ESTATICOS), name='static')


# ----------------------------------------------------------------------------- utilidades
_cliente = None


def cliente():
    global _cliente
    if _cliente is None:
        # Sin sesión: con sesión, ClickHouse rechaza dos consultas a la vez y la página pide varias
        _cliente = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                                 database=DATABASE, autogenerate_session_id=False)
    return _cliente


def filas(sql, **parametros):
    try:
        return list(cliente().query(sql, parameters=parametros).named_results())
    except Exception as e:
        detalle = str(e).split('\n')[0][:200]
        raise HTTPException(503, 'No se pueden leer los datos. ¿Está ClickHouse levantado y se ha ejecutado '
                                 f'el pipeline (python main.py)? Detalle: {detalle}')


def preparar_tablas():
    """Las dos tablas en las que escribe la web (por si aún no las ha creado el pipeline)."""
    from integrar_feedback import crear_tabla_revision_web
    crear_tabla_revision_web(cliente())
    cliente().command('''
        CREATE TABLE IF NOT EXISTS aviso_leido (aviso_id UInt64, comercio UInt32, leido DateTime DEFAULT now())
        ENGINE = MergeTree ORDER BY (aviso_id, comercio)''')


_preparadas = False


def reloj():
    """(hoy, ahora): el último día puntuado y el final de ese día (la hora de los datos)."""
    global _preparadas
    if not _preparadas:
        try:
            preparar_tablas()
            _preparadas = True
        except Exception:
            pass
    f = filas('SELECT max(dia) AS hoy, min(dia) AS inicio FROM riesgo_transaccion')[0]
    if f['hoy'].year < 2000:
        raise HTTPException(503, 'Aún no hay transacciones puntuadas: ejecuta antes el pipeline (python main.py)')
    return f['hoy'], datetime.combine(f['hoy'] + timedelta(days=1), datetime.min.time()), f['inicio']


Comercio = Query(0, ge=0, description='0 = todos los comercios (vista de la entidad)')
Dias = Query(30, ge=0, le=3650, description='últimos N días (0 = todo)')


def desde_de(hoy, dias):
    return hoy - timedelta(days=dias - 1) if dias else datetime(1970, 1, 1).date()


# Base de todo: cada alerta con su transacción, su caso (tarjeta + día) y quién la ha revisado
SQL_ALERTAS = '''
SELECT a.transaccion_id AS transaccion_id, a.dia AS dia, a.prioridad AS prioridad, a.score AS score,
       a.pct_dia AS pct_dia, a.reglas AS reglas, te.timestamp AS timestamp, te.metodo_id AS metodo_id,
       assumeNotNull(te.cliente_id) AS cliente_id, assumeNotNull(te.comercio_id) AS comercio_id,
       te.comercio_nombre AS comercio, te.canal_tipo AS canal, toFloat64(te.cantidad) AS importe,
       te.estado AS estado_pago, mp.numero_enmascarado AS tarjeta,
       w.veredicto AS veredicto_web, w.usuario AS usuario_web, w.comentario AS comentario_web,
       if(f.fecha_revision <= {ahora:DateTime}, f.veredicto, '') AS veredicto_equipo,
       ifNull(te.fecha_confirmacion <= {ahora:DateTime}, 0) AS confirmado_banco,
       te.es_fraude AS real_fraude, te.tipo_fraude AS real_tipo, te.ip_pais AS ip_pais,
       v.dispositivo_nuevo AS dispositivo_nuevo, v.ip_extranjera AS ip_extranjera, v.proxy_vpn AS proxy_vpn,
       v.n_tarjeta_10min AS n_tarjeta_10min, v.n_tarjeta_1h AS n_tarjeta_1h,
       v.n_comercios_cliente_3h AS n_comercios_cliente_3h, v.ratio_importe_habitual AS ratio_importe_habitual,
       v.pct_limite_credito AS pct_limite_credito, v.min_seg_entre_eventos AS min_seg_entre_eventos,
       v.num_intentos_login AS num_intentos_login, v.cambio_dato_sesion AS cambio_dato_sesion,
       v.n_devoluciones_30d AS n_devoluciones_30d, v.es_madrugada AS es_madrugada, v.rechazada AS rechazada
FROM alertas_unificadas AS a
INNER JOIN transacciones_enriquecidas AS te ON a.transaccion_id = te.transaccion_id
INNER JOIN features_transaccion AS v ON v.transaccion_id = a.transaccion_id
LEFT JOIN metodo_pago AS mp ON te.metodo_id = mp.metodo_id
LEFT JOIN (SELECT transaccion_id, argMax(veredicto, fecha_real) AS veredicto, argMax(usuario, fecha_real) AS usuario,
                  argMax(comentario, fecha_real) AS comentario
           FROM revision_web GROUP BY transaccion_id) AS w ON w.transaccion_id = a.transaccion_id
LEFT JOIN (SELECT transaccion_id, any(veredicto) AS veredicto, any(fecha_revision) AS fecha_revision
           FROM feedback_analistas GROUP BY transaccion_id) AS f ON f.transaccion_id = a.transaccion_id
WHERE a.dia >= {desde:Date} AND ({comercio:UInt32} = 0 OR te.comercio_id = {comercio:UInt32})
'''


def estado(f, hoy):
    """Estado de un pago: lo último que se sabe de él (el contracargo del banco manda sobre todo).

    Abiertos: pendiente (alto o crítico, con acción recomendada) o vigilancia (medio). Cerrados:
    fraude, legitima o sin_incidencia (DIAS_CIERRE días sin revisión ni contracargo)."""
    if f.get('confirmado_banco'):
        return 'fraude', 'confirmado por el banco (contracargo)'
    if f.get('veredicto_web'):
        return f['veredicto_web'], f"revisado por {f['usuario_web'] or 'el equipo'}"
    if f.get('veredicto_equipo'):
        return f['veredicto_equipo'], 'revisado por el equipo de analistas'
    if (hoy - f['timestamp'].date()).days >= DIAS_CIERRE:
        return 'sin_incidencia', f'cerrado solo: {DIAS_CIERRE} días sin reclamación'
    if (f.get('prioridad') or 0) >= 2:
        return 'pendiente', 'a la espera de que se resuelva'
    return 'vigilancia', f'en vigilancia: se cierra solo si en {DIAS_CIERRE} días no hay reclamación'


def estado_caso(txs):
    """Estado de un caso. Si un pago es fraude, el caso es fraude; si no, lo deciden sus pagos más
    graves (un pago medio sin revisar no deja abierto un caso crítico ya resuelto)."""
    fraudes = [t for t in txs if t['estado'] == 'fraude']
    if fraudes:
        return 'fraude', fraudes[0]['estado_detalle']
    maximo = max(t['prioridad'] for t in txs)
    principales = [t for t in txs if t['prioridad'] == maximo]
    if any(t['estado'] in ABIERTOS for t in principales):
        return ('pendiente', 'a la espera de que se resuelva') if maximo >= 2 else \
            ('vigilancia', f'en vigilancia: se cierra solo si en {DIAS_CIERRE} días no hay reclamación')
    legitimas = [t for t in principales if t['estado'] == 'legitima']
    return ('legitima', legitimas[0]['estado_detalle']) if legitimas else \
        ('sin_incidencia', principales[0]['estado_detalle'])


def limpiar(f, demo, hoy):
    """Quita lo interno; la verdad del simulador solo en modo demostración."""
    e, origen = estado(f, hoy)
    señales = senales(f)                        # solo lo que dicen las variables (las reglas van aparte)
    salida = {k: v for k, v in f.items()
              if k not in VARIABLES and not k.startswith(('veredicto_', 'usuario_', 'confirmado', 'real_'))}
    salida['senales'] = señales
    salida.update(nivel=NIVEL[f['prioridad']], estado=e, estado_detalle=origen,
                  comentario=f.get('comentario_web') or '')
    salida.pop('comentario_web', None)
    if demo:
        salida['real'] = f['real_tipo'] if f['real_fraude'] else 'legitima'
    return salida


def casos_de(alertas, comercio=0):
    """Agrupa las alertas en casos: misma tarjeta, mismo día."""
    grupos = {}
    for a in alertas:
        grupos.setdefault((a['metodo_id'], a['dia']), []).append(a)
    casos = []
    for (metodo_id, dia), txs in grupos.items():
        prioridad = max(t['prioridad'] for t in txs)
        reglas = sorted({r for t in txs for r in t['reglas']})
        e, detalle = estado_caso(txs)
        casos.append({
            'caso': f'{metodo_id}-{dia}', 'metodo_id': metodo_id, 'dia': dia, 'tarjeta': txs[0]['tarjeta'],
            'cliente_id': txs[0]['cliente_id'], 'prioridad': prioridad, 'nivel': NIVEL[prioridad],
            'percentil': min(t['pct_dia'] for t in txs),     # el puesto del pago más sospechoso (0 = el primero)
            'transacciones': len(txs), 'importe': round(sum(t['importe'] for t in txs), 2),
            'comercios': sorted({t['comercio'] for t in txs}),
            'desde': min(t['timestamp'] for t in txs), 'hasta': max(t['timestamp'] for t in txs),
            'motivos': (reglas + [m for m in dict.fromkeys(s for t in txs for s in t['senales'])
                                  if m not in reglas]) or [MOTIVO_MODELO],
            'estado': e, 'estado_detalle': detalle, 'accion': accion(NIVEL[prioridad], comercio)['corta'],
            **({'real': sorted({t['real'] for t in txs})} if 'real' in txs[0] else {}),
        })
    return casos


ORDENES = {   # cómo ordenar los casos: lo más probable de fraude, lo más reciente o lo de más importe
    'riesgo': lambda c: (-c['prioridad'], c['percentil'], -c['importe']),
    'reciente': lambda c: (-c['hasta'].timestamp(), -c['prioridad']),
    'importe': lambda c: (-c['importe'], -c['prioridad']),
}
Orden = Query('riesgo', description='riesgo (lo más probable de fraude primero), reciente o importe')

_probabilidades = {}


def probabilidades(hoy, ahora):
    """Qué parte de los casos de cada nivel ha resultado ser fraude (entidad entera, casos cerrados).

    Se usan los casos de hace más de DIAS_CIERRE días, que ya están todos cerrados: con los recientes
    saldría más alta (el fraude se confirma antes de que una alerta legítima se cierre sola). Si aún no
    hay bastantes (sistema recién puesto en marcha), se usan todos los cerrados."""
    if ahora not in _probabilidades:
        todas = [limpiar(f, False, hoy) for f in filas(SQL_ALERTAS, ahora=ahora, desde=date(1970, 1, 1), comercio=0)]
        casos = casos_de(todas)
        limite = hoy - timedelta(days=DIAS_CIERRE)
        salida = {}
        for n in NIVELES:
            cerrados = [c for c in casos if c['nivel'] == n and c['estado'] not in ABIERTOS]
            maduros = [c for c in cerrados if c['dia'] <= limite]
            base = maduros if len(maduros) >= 20 else cerrados
            fraude = sum(c['estado'] == 'fraude' for c in base)
            salida[n] = {'prob': round(fraude / len(base), 3) if len(base) >= 10 else None, 'casos': len(base)}
        _probabilidades.clear()
        _probabilidades[ahora] = salida
    return _probabilidades[ahora]


# ----------------------------------------------------------------------------------- página
@app.get('/', include_in_schema=False)
def pagina():
    # no-cache: el navegador pregunta siempre si la página ha cambiado. Si no, tras actualizar la web
    # seguiría enseñando la versión vieja (los .js y .css ya llevan ?v=N en index.html)
    return FileResponse(ESTATICOS / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/api/salud', include_in_schema=False)
def salud():
    """Para las sondas de Kubernetes (k8s/web.yaml): 200 si ClickHouse responde, 503 si no.
    No exige que el pipeline se haya ejecutado: sin datos la página ya avisa en cada pestaña."""
    try:
        cliente().command('SELECT 1')
    except Exception as e:
        raise HTTPException(503, f'ClickHouse no responde: {str(e).splitlines()[0][:200]}')
    return {'estado': 'ok'}


@app.get('/api/contexto')
def contexto():
    """La hora de los datos y la lista de comercios para el selector."""
    hoy, ahora, inicio = reloj()
    comercios = filas('SELECT comercio_id AS id, nombre, categoria_negocio AS categoria FROM comercio ORDER BY nombre')
    return {'hoy': hoy, 'ahora': ahora, 'inicio': inicio, 'comercios': comercios, 'niveles': NIVELES,
            'web_modelos': os.getenv('WEB_MODELOS_URL')}   # sin ella, el enlace va al puerto 8001


# ---------------------------------------------------------------------------------- el día
@app.get('/api/dia')
def dia_en_una_linea(comercio: int = Comercio):
    """Los pagos del último día puntuado, de menos a más riesgo: el "código de barras" del parte.

    Cada barra es [altura, prioridad, metodo_id, importe]. La altura es la raíz de la nota del modelo
    respecto a la más alta del día: las notas son probabilidades muy sesgadas (la mediana ronda el 1 %)
    y con la raíz se distinguen también las intermedias."""
    hoy, _, _ = reloj()
    pagos = filas('''
        SELECT r.score AS nota, a.prioridad AS prioridad, te.metodo_id AS metodo_id,
               te.comercio_id AS comercio_id, toFloat64(te.cantidad) AS importe
        FROM riesgo_transaccion AS r FINAL
        INNER JOIN transacciones_enriquecidas AS te ON te.transaccion_id = r.transaccion_id
        LEFT JOIN alertas_unificadas AS a ON a.transaccion_id = r.transaccion_id
        WHERE r.dia = {dia:Date} AND ({comercio:UInt32} = 0 OR te.comercio_id = {comercio:UInt32})
        ORDER BY r.posicion DESC''', dia=hoy, comercio=comercio)
    maximo = max((max(p['nota'], 0) for p in pagos), default=0) or 1
    return {
        'dia': hoy, 'pagos': len(pagos), 'comercios': len({p['comercio_id'] for p in pagos}),
        'barras': [[round((max(p['nota'], 0) / maximo) ** 0.5, 3), p['prioridad'] or 0, p['metodo_id'],
                    round(p['importe'], 2)] for p in pagos],
    }


# ---------------------------------------------------------------------------------- alertas
@app.get('/api/alertas')
def alertas(comercio: int = Comercio, dias: int = Dias,
            estado_filtro: Literal['pendientes', 'revisadas', 'todas'] = Query('pendientes', alias='estado'),
            nivel_minimo: int = Query(1, ge=1, le=3, alias='nivel'),
            orden: Literal['riesgo', 'reciente', 'importe'] = Orden, demo: bool = False):
    """Casos de alerta (tarjeta + día) y el resumen de los abiertos.

    estado: pendientes = sin cerrar (pendiente o en vigilancia), revisadas = cerrados."""
    hoy, ahora, _ = reloj()
    todas = [limpiar(f, demo, hoy) for f in filas(SQL_ALERTAS, ahora=ahora, desde=desde_de(hoy, dias),
                                                  comercio=comercio)]
    todos_los_casos = casos_de(todas, comercio)
    casos = [c for c in todos_los_casos if c['prioridad'] >= nivel_minimo]
    abiertos = [c for c in casos if c['estado'] in ABIERTOS]
    resumen = {   # por nivel, de todos los abiertos; el número y el importe, de los que se ven
        'abiertos': len(abiertos),
        'por_nivel': {n: sum(c['nivel'] == n and c['estado'] in ABIERTOS for c in todos_los_casos) for n in NIVELES},
        'total_por_nivel': {n: sum(c['nivel'] == n for c in todos_los_casos) for n in NIVELES},   # del periodo
        'cerrados': {e: sum(c['estado'] == e for c in todos_los_casos) for e in ('fraude', 'legitima', 'sin_incidencia')},
        'importe_en_riesgo': round(sum(c['importe'] for c in abiertos if c['estado'] == 'pendiente'), 2),
        'probabilidad': probabilidades(hoy, ahora),
        'acciones': {n: accion(n, comercio)['corta'] for n in NIVELES},
        'dias_cierre': DIAS_CIERRE,
    }
    if estado_filtro == 'pendientes':
        casos = abiertos
    elif estado_filtro == 'revisadas':
        casos = [c for c in casos if c['estado'] not in ABIERTOS]
    casos.sort(key=ORDENES[orden])
    return {'resumen': resumen, 'casos': casos}


# Motivos en lenguaje claro: reglas y señales de las variables (el puesto en el día va aparte: percentil)
def motivos(v, reglas):
    return [f'Regla: {r}' for r in reglas] + senales(v)


SQL_TRANSACCIONES = '''
SELECT te.transaccion_id AS transaccion_id, te.timestamp AS timestamp, te.metodo_id AS metodo_id,
       assumeNotNull(te.cliente_id) AS cliente_id, assumeNotNull(te.comercio_id) AS comercio_id,
       te.comercio_nombre AS comercio, te.categoria_negocio AS categoria, te.canal_tipo AS canal,
       toFloat64(te.cantidad) AS importe, te.estado AS estado_pago, te.dispositivo_tipo AS dispositivo,
       te.ip_pais AS ip_pais, mp.numero_enmascarado AS tarjeta,
       a.prioridad AS prioridad, a.pct_dia AS pct_dia, a.reglas AS reglas, r.pct AS percentil,
       w.veredicto AS veredicto_web, w.usuario AS usuario_web, w.comentario AS comentario_web,
       if(f.fecha_revision <= {ahora:DateTime}, f.veredicto, '') AS veredicto_equipo,
       ifNull(te.fecha_confirmacion <= {ahora:DateTime}, 0) AS confirmado_banco,
       te.es_fraude AS real_fraude, te.tipo_fraude AS real_tipo,
       v.dispositivo_nuevo AS dispositivo_nuevo, v.ip_extranjera AS ip_extranjera, v.proxy_vpn AS proxy_vpn,
       v.n_tarjeta_10min AS n_tarjeta_10min, v.n_tarjeta_1h AS n_tarjeta_1h,
       v.n_comercios_cliente_3h AS n_comercios_cliente_3h, v.ratio_importe_habitual AS ratio_importe_habitual,
       v.pct_limite_credito AS pct_limite_credito, v.min_seg_entre_eventos AS min_seg_entre_eventos,
       v.num_intentos_login AS num_intentos_login, v.cambio_dato_sesion AS cambio_dato_sesion,
       v.n_devoluciones_30d AS n_devoluciones_30d, v.es_madrugada AS es_madrugada, v.rechazada AS rechazada
FROM transacciones_enriquecidas AS te
INNER JOIN features_transaccion AS v ON v.transaccion_id = te.transaccion_id
LEFT JOIN metodo_pago AS mp ON te.metodo_id = mp.metodo_id
LEFT JOIN alertas_unificadas AS a ON a.transaccion_id = te.transaccion_id
LEFT JOIN (SELECT transaccion_id, 1 AS puntuada, posicion / transacciones_dia AS pct FROM riesgo_transaccion FINAL) AS r
       ON r.transaccion_id = te.transaccion_id
LEFT JOIN (SELECT transaccion_id, argMax(veredicto, fecha_real) AS veredicto, argMax(usuario, fecha_real) AS usuario,
                  argMax(comentario, fecha_real) AS comentario
           FROM revision_web GROUP BY transaccion_id) AS w ON w.transaccion_id = te.transaccion_id
LEFT JOIN (SELECT transaccion_id, any(veredicto) AS veredicto, any(fecha_revision) AS fecha_revision
           FROM feedback_analistas GROUP BY transaccion_id) AS f ON f.transaccion_id = te.transaccion_id
WHERE {filtro}
ORDER BY {orden}
LIMIT {limite:UInt32}
'''

ORDEN_SQL = {   # mismo criterio que ORDENES; las transacciones sin puntuar van al final al ordenar por riesgo
    'riesgo': 'a.prioridad DESC, r.puntuada DESC, r.pct ASC, te.timestamp DESC',
    'reciente': 'te.timestamp DESC, te.transaccion_id DESC',
    'importe': 'te.cantidad DESC, te.timestamp DESC',
}


def sql_transacciones(filtro, orden='reciente'):
    return SQL_TRANSACCIONES.replace('{filtro}', filtro).replace('{orden}', ORDEN_SQL[orden])


def ficha_transaccion(f, demo, hoy):
    """Una transacción para la web: nivel (si es alerta), estado, motivos."""
    prioridad = f['prioridad'] or 0
    salida = {k: f[k] for k in ('transaccion_id', 'timestamp', 'metodo_id', 'cliente_id', 'comercio_id', 'comercio',
                                'categoria', 'canal', 'importe', 'estado_pago', 'dispositivo', 'ip_pais', 'tarjeta')}
    salida['prioridad'] = prioridad
    salida['nivel'] = NIVEL.get(prioridad, '')
    salida['percentil'] = f['percentil'] if prioridad else None
    e, origen = estado(f, hoy)
    salida['estado'] = e if prioridad or e in ('fraude', 'legitima') else ''
    salida['estado_detalle'] = origen if salida['estado'] else ''
    salida['comentario'] = f['comentario_web'] or ''
    salida['motivos'] = motivos(f, f['reglas'] or []) if prioridad else []
    if demo:
        salida['real'] = f['real_tipo'] if f['real_fraude'] else 'legitima'
    return salida


@app.get('/api/caso')
def caso(metodo_id: int, dia: str, comercio: int = Comercio, demo: bool = False):
    """Ficha de un caso: qué hacer, los pagos de la tarjeta ese día (con motivos) y su historial.

    En la vista de un comercio solo salen sus pagos y no los datos personales del titular."""
    hoy, ahora, _ = reloj()
    try:
        d = datetime.strptime(dia, '%Y-%m-%d').date()
    except ValueError:
        raise HTTPException(422, 'dia tiene que ser AAAA-MM-DD')
    del_comercio = ' AND te.comercio_id = {comercio:UInt32}' if comercio else ''
    filtro = 'te.metodo_id = {metodo:UInt32} AND toDate(te.timestamp) = {dia:Date}' + del_comercio
    del_dia = [ficha_transaccion(f, demo, hoy) for f in filas(sql_transacciones(filtro), ahora=ahora,
                                                            metodo=metodo_id, dia=d, comercio=comercio, limite=200)]
    if not del_dia:
        raise HTTPException(404, 'No hay transacciones de esa tarjeta ese día')
    historial = [ficha_transaccion(f, demo, hoy) for f in filas(
        sql_transacciones('te.metodo_id = {metodo:UInt32} AND toDate(te.timestamp) < {dia:Date}' + del_comercio),
        ahora=ahora, metodo=metodo_id, dia=d, comercio=comercio, limite=15)]
    tarjeta = filas('''
        SELECT mp.numero_enmascarado AS tarjeta, mp.tipo AS tipo, mp.entidad_emisora AS entidad,
               toFloat64(ifNull(mp.limite_credito, 0)) AS limite, c.cliente_id AS cliente_id, c.nombre_completo AS cliente,
               c.pais AS pais, c.fecha_alta AS alta
        FROM metodo_pago AS mp INNER JOIN cliente AS c ON mp.cliente_id = c.cliente_id
        WHERE mp.metodo_id = {m:UInt32}''', m=metodo_id)
    tarjeta = tarjeta[0] if tarjeta else None
    if tarjeta and comercio:
        tarjeta = {k: tarjeta[k] for k in ('tarjeta', 'tipo', 'cliente_id', 'pais')}
    alertadas = [t for t in del_dia if t['nivel']]
    salida = {'metodo_id': metodo_id, 'dia': d, 'tarjeta': tarjeta, 'vista': 'comercio' if comercio else 'entidad',
              'nivel': '', 'transacciones': del_dia[::-1], 'historial': historial}
    if alertadas:
        nivel = NIVEL[max(t['prioridad'] for t in alertadas)]
        e, detalle = estado_caso(alertadas)
        salida.update(nivel=nivel, estado=e, estado_detalle=detalle, accion=accion(nivel, comercio),
                      probabilidad=probabilidades(hoy, ahora)[nivel], dias_cierre=DIAS_CIERRE)
    return salida


class Revision(BaseModel):
    transaccion_ids: list[int] = Field(min_length=1, max_length=200)
    veredicto: Literal['fraude', 'legitima']
    comentario: str = Field('', max_length=1000)
    usuario: str = Field('analista', min_length=1, max_length=80)
    comercio: int = 0


@app.post('/api/revisiones')
def revisar(r: Revision):
    """Guarda el veredicto de una persona. El recalibrado semanal lo usa como feedback."""
    _, ahora, _ = reloj()
    existentes = {f['transaccion_id'] for f in filas(   # un comercio solo puede opinar de sus pagos
        'SELECT transaccion_id FROM transacciones_enriquecidas WHERE transaccion_id IN {ids:Array(UInt32)} '
        'AND ({comercio:UInt32} = 0 OR comercio_id = {comercio:UInt32})', ids=r.transaccion_ids, comercio=r.comercio)}
    faltan = set(r.transaccion_ids) - existentes
    if faltan:
        raise HTTPException(404, f'No existen estas transacciones (o no son de este comercio): {sorted(faltan)[:10]}')
    try:
        cliente().insert('revision_web',
                         [[t, r.veredicto, r.comentario.strip(), r.usuario.strip(), r.comercio, ahora]
                          for t in r.transaccion_ids],
                         column_names=['transaccion_id', 'veredicto', 'comentario', 'usuario', 'comercio',
                                       'fecha_revision'])
    except Exception as e:
        raise HTTPException(503, f'No se ha podido guardar la revisión: {str(e)[:200]}')
    _probabilidades.clear()
    return {'guardadas': len(r.transaccion_ids), 'veredicto': r.veredicto}


# ---------------------------------------------------------------------------------- resumen
@app.get('/api/resumen')
def resumen(comercio: int = Comercio, dias: int = Dias):
    """Lo que interesa al responsable: cuánto se alerta, cuánto fraude se confirma y cuánto acierta."""
    hoy, ahora, inicio = reloj()
    desde = max(desde_de(hoy, dias), inicio)
    alertas_ = [limpiar(f, False, hoy) for f in filas(SQL_ALERTAS, ahora=ahora, desde=desde, comercio=comercio)]
    casos = casos_de(alertas_, comercio)
    # Fraude confirmado (banco o personas) entre las transacciones del periodo, con alerta o sin ella
    conf = filas('''
        SELECT toDate(te.timestamp) AS dia, count() AS fraudes, toFloat64(sum(te.cantidad)) AS importe,
               countIf(a.transaccion_id != 0) AS con_alerta,
               toFloat64(sumIf(te.cantidad, a.transaccion_id != 0)) AS importe_con_alerta
        FROM transacciones_enriquecidas AS te
        LEFT JOIN alertas_unificadas AS a ON a.transaccion_id = te.transaccion_id
        LEFT JOIN (SELECT transaccion_id, argMax(veredicto, fecha_real) AS veredicto FROM revision_web
                   GROUP BY transaccion_id) AS w ON w.transaccion_id = te.transaccion_id
        LEFT JOIN (SELECT transaccion_id, any(veredicto) AS veredicto, any(fecha_revision) AS fecha_revision
                   FROM feedback_analistas GROUP BY transaccion_id) AS f ON f.transaccion_id = te.transaccion_id
        WHERE toDate(te.timestamp) BETWEEN {desde:Date} AND {hoy:Date}
          AND ({comercio:UInt32} = 0 OR te.comercio_id = {comercio:UInt32})
          AND multiIf(w.veredicto != '', w.veredicto = 'fraude',
                      f.veredicto != '' AND f.fecha_revision <= {ahora:DateTime}, f.veredicto = 'fraude',
                      ifNull(te.fecha_confirmacion <= {ahora:DateTime}, 0))
        GROUP BY dia ORDER BY dia
    ''', desde=desde, hoy=hoy, ahora=ahora, comercio=comercio)
    graves_cerrados = [c for c in casos if c['prioridad'] >= 2 and c['estado'] not in ABIERTOS]
    fraudes = sum(c['fraudes'] for c in conf)
    importe_fraude = sum(c['importe'] for c in conf)
    por_dia = {}
    vacio = lambda dia: {'dia': dia, **{n: 0 for n in NIVELES}, 'fraude': 0}
    for c in casos:
        por_dia.setdefault(c['dia'], vacio(c['dia']))[c['nivel']] += 1
    for c in conf:
        por_dia.setdefault(c['dia'], vacio(c['dia']))['fraude'] = c['fraudes']
    comercios = {}
    for a in alertas_:
        x = comercios.setdefault(a['comercio'], {'comercio': a['comercio'], 'alertas': 0, 'fraude': 0})
        x['alertas'] += 1
        x['fraude'] += a['estado'] == 'fraude'
    return {
        'desde': desde, 'hoy': hoy,
        'casos': len(casos), 'abiertos': sum(c['estado'] in ABIERTOS for c in casos),
        'criticos_abiertos': sum(c['estado'] in ABIERTOS and c['nivel'] == 'critico' for c in casos),
        'importe_en_riesgo': round(sum(c['importe'] for c in casos if c['estado'] == 'pendiente'), 2),
        'fraudes_confirmados': fraudes, 'importe_fraude': round(importe_fraude, 2),
        # Cobertura: qué parte del fraude confirmado tuvo alerta, en número de pagos y en dinero
        'fraudes_con_alerta': sum(c['con_alerta'] for c in conf),
        'importe_con_alerta': round(sum(c['importe_con_alerta'] for c in conf), 2),
        'cobertura_pagos': sum(c['con_alerta'] for c in conf) / fraudes if fraudes else None,
        'cobertura_importe': sum(c['importe_con_alerta'] for c in conf) / importe_fraude if importe_fraude else None,
        'acierto_graves': (sum(c['estado'] == 'fraude' for c in graves_cerrados) / len(graves_cerrados)
                           if graves_cerrados else None),
        'graves_cerrados': len(graves_cerrados),
        'estados': {e: sum(c['estado'] == e for c in casos)
                    for e in ('pendiente', 'vigilancia', 'fraude', 'legitima', 'sin_incidencia')},
        'por_dia': sorted(por_dia.values(), key=lambda d: d['dia']),
        'comercios': sorted(comercios.values(), key=lambda x: -x['alertas'])[:8],
    }


# ---------------------------------------------------------------------------- transacciones
@app.get('/api/transacciones')
def transacciones(comercio: int = Comercio, q: str = Query('', max_length=60), limite: int = Query(50, ge=1, le=500),
                  solo_alertas: bool = False, orden: Literal['riesgo', 'reciente', 'importe'] = Query('reciente'),
                  demo: bool = False):
    """Buscador: por id de transacción o de cliente, últimos dígitos de tarjeta o nombre de comercio."""
    hoy, ahora, _ = reloj()
    condiciones = ['({comercio:UInt32} = 0 OR te.comercio_id = {comercio:UInt32})']
    parametros = {'ahora': ahora, 'comercio': comercio, 'limite': limite}
    q = q.strip()
    if q:
        if q.isdigit():
            condiciones.append('(te.transaccion_id = {n:UInt64} OR te.cliente_id = {n:UInt64} '
                               'OR endsWith(mp.numero_enmascarado, {q:String}))')
            parametros['n'] = int(q)
        else:
            condiciones.append('positionCaseInsensitiveUTF8(te.comercio_nombre, {q:String}) > 0')
        parametros['q'] = q
    if solo_alertas:
        condiciones.append('a.prioridad > 0')
    sql = sql_transacciones(' AND '.join(condiciones), orden)
    return [ficha_transaccion(f, demo, hoy) for f in filas(sql, **parametros)]


# ----------------------------------------------------------------------------------- avisos
@app.get('/api/avisos')
def avisos(comercio: int = Comercio, limite: int = Query(30, ge=1, le=500), solo_no_leidos: bool = False):
    """Avisos de casos graves (los crea generar_avisos.py tras cada scoring).

    Los de hace más de una semana se dan por archivados: cuentan como leídos."""
    hoy, ahora, _ = reloj()
    leido = "(l.leido = 1 OR av.fecha < {ahora:DateTime} - INTERVAL 7 DAY)"
    condicion = '({comercio:UInt32} = 0 OR has(av.comercios, {comercio:UInt32}))'
    base = f'''
        FROM aviso AS av FINAL
        LEFT JOIN (SELECT DISTINCT aviso_id, 1 AS leido FROM aviso_leido WHERE comercio = {{comercio:UInt32}}) AS l
               ON l.aviso_id = av.aviso_id
        WHERE {condicion}'''
    no_leidos = filas(f'SELECT count() AS n, countIf(av.nivel = \'critico\') AS criticos {base} AND NOT {leido}',
                      comercio=comercio, ahora=ahora)[0]
    lista = filas(f'''
        SELECT toString(av.aviso_id) AS aviso_id, av.fecha AS fecha, av.dia AS dia, av.metodo_id AS metodo_id,
               av.nivel AS nivel, av.transacciones AS transacciones, av.importe AS importe, av.mensaje AS mensaje,
               {leido} AS leido
        {base} {'AND NOT ' + leido if solo_no_leidos else ''}
        ORDER BY av.fecha DESC, av.nivel = 'critico' DESC, av.importe DESC
        LIMIT {{limite:UInt32}}''', comercio=comercio, limite=limite, ahora=ahora)
    # hoy: la página lo compara en cada comprobación para saber si el pipeline ha puntuado un día nuevo
    return {'hoy': hoy, 'no_leidos': no_leidos['n'], 'criticos_no_leidos': no_leidos['criticos'], 'avisos': lista}


class Leidos(BaseModel):
    aviso_ids: list[str] = Field(default_factory=list, max_length=1000)
    todos: bool = False
    comercio: int = 0


@app.post('/api/avisos/leidos')
def marcar_leidos(l: Leidos):
    reloj()
    if l.todos:
        ids = [int(f['id']) for f in filas(
            'SELECT aviso_id AS id FROM aviso FINAL WHERE {c:UInt32} = 0 OR has(comercios, {c:UInt32})', c=l.comercio)]
    else:
        try:
            ids = [int(i) for i in l.aviso_ids]
        except ValueError:
            raise HTTPException(422, 'aviso_ids tienen que ser números')
    if ids:
        cliente().insert('aviso_leido', [[i, l.comercio] for i in ids], column_names=['aviso_id', 'comercio'])
    return {'marcados': len(ids)}


# --------------------------------------------------------------------------------- patrones
# Patrones de fraude por momento del día, dispositivo, comercio y cliente (la pestaña Resumen, debajo
# de las cifras). Cuenta como fraude lo mismo que el resumen: lo que el banco o una revisión han
# confirmado hasta "ahora", nunca la verdad del simulador.
SQL_PATRONES = '''
WITH base AS (
    SELECT toHour(te.timestamp) AS hora, toDayOfWeek(te.timestamp) AS dia_semana,
           ifNull(te.dispositivo_tipo, 'presencial') AS dispositivo, v.dispositivo_nuevo AS dispositivo_nuevo,
           te.cliente_id AS cliente_id, te.comercio_nombre AS comercio, toFloat64(te.cantidad) AS importe,
           toUInt8(multiIf(w.veredicto != '', w.veredicto = 'fraude',
                           f.veredicto != '' AND f.fecha_revision <= {ahora:DateTime}, f.veredicto = 'fraude',
                           ifNull(te.fecha_confirmacion <= {ahora:DateTime}, 0))) AS fraude
    FROM transacciones_enriquecidas AS te
    LEFT JOIN features_transaccion AS v ON v.transaccion_id = te.transaccion_id
    LEFT JOIN (SELECT transaccion_id, argMax(veredicto, fecha_real) AS veredicto FROM revision_web
               GROUP BY transaccion_id) AS w ON w.transaccion_id = te.transaccion_id
    LEFT JOIN (SELECT transaccion_id, any(veredicto) AS veredicto, any(fecha_revision) AS fecha_revision
               FROM feedback_analistas GROUP BY transaccion_id) AS f ON f.transaccion_id = te.transaccion_id
    WHERE toDate(te.timestamp) BETWEEN {desde:Date} AND {hoy:Date}
      AND ({comercio:UInt32} = 0 OR te.comercio_id = {comercio:UInt32}))
SELECT {columnas} FROM base {resto}
'''


def por(columnas, resto='', **p):
    return filas(SQL_PATRONES.replace('{columnas}', columnas).replace('{resto}', resto), **p)


@app.get('/api/patrones')
def patrones(comercio: int = Comercio, dias: int = Dias):
    """Dónde se concentra el fraude confirmado: hora del día, día de la semana, dispositivo,
    comercio (solo en la vista de la entidad) y clientes con más fraude. A diferencia del resumen,
    mira todo el histórico de pagos, también el anterior a la primera alerta."""
    hoy, ahora, _ = reloj()
    p = {'desde': desde_de(hoy, dias), 'hoy': hoy, 'ahora': ahora, 'comercio': comercio}
    cuenta = 'count() AS pagos, toUInt32(sum(fraude)) AS fraudes'
    total = por(cuenta, **p)[0]
    clientes_fraude = por('uniqExactIf(cliente_id, fraude = 1) AS con_fraude, uniqExact(cliente_id) AS clientes', **p)[0]
    return {
        'desde': p['desde'], 'hoy': hoy, **total,
        'tasa': total['fraudes'] / total['pagos'] if total['pagos'] else None,
        'por_hora': por(f'hora, {cuenta}', 'GROUP BY hora ORDER BY hora', **p),
        'por_dia_semana': por(f'dia_semana, {cuenta}', 'GROUP BY dia_semana ORDER BY dia_semana', **p),
        'por_dispositivo': por(f"dispositivo, if(dispositivo = 'presencial', 0, dispositivo_nuevo) AS nuevo, {cuenta}",
                               'GROUP BY dispositivo, nuevo ORDER BY pagos DESC', **p),
        'por_comercio': [] if comercio else por(f'comercio, {cuenta}', 'GROUP BY comercio ORDER BY fraudes / pagos DESC', **p),
        # Solo el número de cliente: en la vista de un comercio no se enseñan datos personales
        'clientes': por(f"cliente_id, {cuenta}, round(sumIf(importe, fraude = 1), 2) AS importe_fraude",
                        'GROUP BY cliente_id HAVING fraudes > 0 ORDER BY fraudes DESC, importe_fraude DESC LIMIT 8', **p),
        'clientes_con_fraude': clientes_fraude['con_fraude'], 'clientes_activos': clientes_fraude['clientes'],
    }


# ---------------------------------------------------------------------------------- ajustes
# Canal de Teams / Slack y correos de los avisos, configurables desde la web (web/ajustes.py)
from web.ajustes import crear_router  # noqa: E402
app.include_router(crear_router(cliente))
