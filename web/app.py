"""
Web del proyecto antifraude (Hito 6): una API JSON que lee ClickHouse y los resultados del
pipeline, y la página que los muestra (web/static/).

    uvicorn web.app:app --reload        # desde la raíz del proyecto -> http://localhost:8000
                                        # documentación de la API: http://localhost:8000/api/docs

Solo lee: no escribe nada en ClickHouse ni en el disco. Necesita el pipeline ejecutado
(python main.py); si falta alguna tabla, esa parte de la página avisa y el resto funciona.
"""
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import clickhouse_connect
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from config import CARPETA_RESULTADOS, CONFIG, DATABASE, HOST, PASSWORD, PORT, USER

ESTATICOS = Path(__file__).resolve().parent / 'static'
MODELO_BASE = CONFIG['feedback']['modelo_base']     # el detector elegido (ordena las alertas)
UMBRAL_ANOMALIA = 0.99                              # score_pct: más rara que el 99 % del entrenamiento
DIAS_SEMANA = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']

app = FastAPI(title='Antifraude MERLIN', docs_url='/api/docs', redoc_url=None)
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
    """Resultado de la consulta como lista de diccionarios. Si falla (ClickHouse apagado, tabla que
    aún no existe...), error 503 con un mensaje que la página enseña en su lugar."""
    try:
        return list(cliente().query(sql, parameters=parametros).named_results())
    except Exception as e:
        detalle = str(e).split('\n')[0][:200]
        raise HTTPException(503, f'No se pueden leer los datos: ¿está ClickHouse levantado y el pipeline '
                                 f'ejecutado (python main.py)? Detalle: {detalle}')


def opcional(sql, **parametros):
    """Como filas(), pero None si falla: para partes del resumen que pueden no existir aún."""
    try:
        return filas(sql, **parametros)
    except HTTPException:
        return None


def periodo(dias):
    """(desde, hasta): los últimos `dias` días completos hasta el de la última transacción
    (dias = 0 -> todo el periodo)."""
    hasta = filas('SELECT max(timestamp) AS t FROM transaccion')[0]['t']
    if hasta.year < 2000:
        raise HTTPException(503, 'No hay transacciones cargadas: ejecuta antes el pipeline (python main.py)')
    desde = datetime.combine(hasta.date() - timedelta(days=dias - 1), time.min) if dias else datetime(1970, 1, 1)
    return desde, hasta


def csv(nombre):
    """Un CSV de resultados/ como lista de diccionarios (None si aún no existe)."""
    ruta = CARPETA_RESULTADOS / nombre
    if not ruta.exists():
        return None
    df = pd.read_csv(ruta)
    return df.astype(object).where(df.notna(), None).to_dict('records')


Dias = Query(0, ge=0, le=3650, description='últimos N días (0 = todo el periodo)')
Limite = Query(25, ge=1, le=500)


# ----------------------------------------------------------------------------------- página
@app.get('/', include_in_schema=False)
def pagina():
    return FileResponse(ESTATICOS / 'index.html')


# ---------------------------------------------------------------------------------- resumen
@app.get('/api/resumen')
def resumen(dias: int = Dias):
    desde, hasta = periodo(dias)
    tx = filas('''
        SELECT count() AS transacciones, toFloat64(sum(cantidad)) AS importe,
               countIf(es_fraude = 1) AS fraudes, toFloat64(sumIf(cantidad, es_fraude = 1)) AS importe_fraude,
               countIf(es_fraude = 1 AND fecha_confirmacion <= {hasta:DateTime}) AS confirmados_banco,
               countIf(estado = 'rechazada') AS rechazadas,
               uniqExact(cliente_id) AS clientes, uniqExact(comercio_id) AS comercios,
               min(timestamp) AS desde, max(timestamp) AS hasta
        FROM transacciones_enriquecidas WHERE timestamp >= {desde:DateTime}
    ''', desde=desde, hasta=hasta)[0]
    reglas = opcional('''
        SELECT count() AS alertas, uniqExact(a.transaccion_id) AS transacciones,
               uniqExactIf(a.transaccion_id, t.es_fraude = 1) AS fraudes
        FROM alerta AS a INNER JOIN transaccion AS t ON a.transaccion_id = t.transaccion_id
        WHERE a.origen = 'regla' AND t.timestamp >= {desde:DateTime}
    ''', desde=desde)
    analistas = opcional('''
        SELECT count() AS revisadas, countIf(f.veredicto = 'fraude') AS veredicto_fraude,
               countIf(t.es_fraude = 1) AS fraude_real
        FROM feedback_analistas AS f INNER JOIN transaccion AS t ON f.transaccion_id = t.transaccion_id
        WHERE t.timestamp >= {desde:DateTime}
    ''', desde=desde)
    anomalias = opcional('''
        SELECT countIf(s.score_pct >= {u:Float64}) AS anomalas,
               countIf(s.score_pct >= {u:Float64} AND t.es_fraude = 1) AS fraudes
        FROM anomaly_scores AS s INNER JOIN transaccion AS t ON s.transaccion_id = t.transaccion_id
        WHERE s.model = {m:String} AND t.timestamp >= {desde:DateTime}
    ''', u=UMBRAL_ANOMALIA, m=MODELO_BASE, desde=desde)
    return {'transacciones': tx, 'reglas': reglas and reglas[0], 'analistas': analistas and analistas[0],
            'anomalias': anomalias and anomalias[0], 'modelo_base': MODELO_BASE,
            'umbral_anomalia': UMBRAL_ANOMALIA}


@app.get('/api/serie')
def serie(dias: int = Dias):
    """Por día: transacciones, fraudes, importe, transacciones con alerta de reglas y anómalas."""
    desde, _ = periodo(dias)
    por_dia = {f['dia']: f for f in filas('''
        SELECT toDate(timestamp) AS dia, count() AS transacciones, countIf(es_fraude = 1) AS fraudes,
               toFloat64(sum(cantidad)) AS importe
        FROM transaccion WHERE timestamp >= {desde:DateTime} GROUP BY dia ORDER BY dia
    ''', desde=desde)}
    extras = {
        'alertas': '''SELECT toDate(t.timestamp) AS dia, uniqExact(a.transaccion_id) AS n
                      FROM alerta AS a INNER JOIN transaccion AS t ON a.transaccion_id = t.transaccion_id
                      WHERE a.origen = 'regla' AND t.timestamp >= {desde:DateTime} GROUP BY dia''',
        'anomalas': '''SELECT toDate(t.timestamp) AS dia, countIf(s.score_pct >= {u:Float64}) AS n
                       FROM anomaly_scores AS s INNER JOIN transaccion AS t ON s.transaccion_id = t.transaccion_id
                       WHERE s.model = {m:String} AND t.timestamp >= {desde:DateTime} GROUP BY dia''',
    }
    for clave, sql in extras.items():
        for f in opcional(sql, desde=desde, u=UMBRAL_ANOMALIA, m=MODELO_BASE) or []:
            if f['dia'] in por_dia:
                por_dia[f['dia']][clave] = f['n']
    return [{'alertas': 0, 'anomalas': 0, **f} for f in por_dia.values()]


# --------------------------------------------------------------------------------- patrones
def tasa_por(columna, desde, tabla='transacciones_enriquecidas', limite=50, nulo='-'):
    return filas(f'''
        SELECT ifNull(toString({columna}), {{nulo:String}}) AS valor, count() AS transacciones,
               countIf(es_fraude = 1) AS fraudes, fraudes / transacciones AS tasa
        FROM {tabla} WHERE timestamp >= {{desde:DateTime}}
        GROUP BY valor ORDER BY transacciones DESC LIMIT {limite}
    ''', desde=desde, nulo=nulo)


@app.get('/api/patrones')
def patrones(dias: int = Dias):
    """Fraude por tipo, temporalidad, canal, comercio, dispositivo, país de la IP, cliente y señales."""
    desde, _ = periodo(dias)
    por_hora = filas('''
        SELECT toHour(timestamp) AS hora, count() AS transacciones, countIf(es_fraude = 1) AS fraudes
        FROM transaccion WHERE timestamp >= {desde:DateTime} GROUP BY hora ORDER BY hora
    ''', desde=desde)
    por_dia_semana = filas('''
        SELECT toDayOfWeek(timestamp) AS dia, count() AS transacciones, countIf(es_fraude = 1) AS fraudes
        FROM transaccion WHERE timestamp >= {desde:DateTime} GROUP BY dia ORDER BY dia
    ''', desde=desde)
    for f in por_dia_semana:
        f['nombre'] = DIAS_SEMANA[f['dia'] - 1]
    por_tipo = filas('''
        SELECT tipo_fraude AS tipo, count() AS transacciones, toFloat64(sum(cantidad)) AS importe
        FROM transaccion WHERE es_fraude = 1 AND timestamp >= {desde:DateTime}
        GROUP BY tipo ORDER BY transacciones DESC
    ''', desde=desde)
    # Señales simples: qué parte de las transacciones con cada señal es fraude
    senales = filas('''
        SELECT countIf(dispositivo_nuevo = 1) AS n_dispositivo_nuevo, countIf(dispositivo_nuevo = 1 AND es_fraude = 1) AS f_dispositivo_nuevo,
               countIf(ip_extranjera = 1) AS n_ip_extranjera, countIf(ip_extranjera = 1 AND es_fraude = 1) AS f_ip_extranjera,
               countIf(proxy_vpn = 1) AS n_proxy_vpn, countIf(proxy_vpn = 1 AND es_fraude = 1) AS f_proxy_vpn,
               countIf(es_madrugada = 1) AS n_madrugada, countIf(es_madrugada = 1 AND es_fraude = 1) AS f_madrugada,
               countIf(rechazada = 1) AS n_rechazada, countIf(rechazada = 1 AND es_fraude = 1) AS f_rechazada,
               countIf(n_tarjeta_1h >= 3) AS n_rafaga, countIf(n_tarjeta_1h >= 3 AND es_fraude = 1) AS f_rafaga,
               count() AS n_todas, countIf(es_fraude = 1) AS f_todas
        FROM features_transaccion WHERE timestamp >= {desde:DateTime}
    ''', desde=desde)[0]
    nombres = {'dispositivo_nuevo': 'Dispositivo nuevo', 'ip_extranjera': 'IP de otro país',
               'proxy_vpn': 'Proxy / VPN', 'madrugada': 'Madrugada (0-6 h)', 'rechazada': 'Pago rechazado',
               'rafaga': '>= 3 pagos de la tarjeta en 1 h', 'todas': 'Todas las transacciones'}
    senales = [{'senal': nombre, 'transacciones': senales[f'n_{k}'], 'fraudes': senales[f'f_{k}'],
                'tasa': senales[f'f_{k}'] / senales[f'n_{k}'] if senales[f'n_{k}'] else 0}
               for k, nombre in nombres.items()]
    top_comercios = filas('''
        SELECT comercio_nombre AS comercio, categoria_negocio AS categoria, count() AS transacciones,
               countIf(es_fraude = 1) AS fraudes, fraudes / transacciones AS tasa
        FROM transacciones_enriquecidas WHERE timestamp >= {desde:DateTime}
        GROUP BY comercio, categoria ORDER BY fraudes DESC, transacciones DESC LIMIT 10
    ''', desde=desde)
    top_clientes = filas('''
        SELECT te.cliente_id AS cliente_id, any(c.nombre_completo) AS nombre, count() AS transacciones,
               countIf(te.es_fraude = 1) AS fraudes, toFloat64(sumIf(te.cantidad, te.es_fraude = 1)) AS importe_fraude,
               groupUniqArrayIf(toString(te.tipo_fraude), te.es_fraude = 1) AS tipos
        FROM transacciones_enriquecidas AS te INNER JOIN cliente AS c ON te.cliente_id = c.cliente_id
        WHERE te.timestamp >= {desde:DateTime}
        GROUP BY cliente_id HAVING fraudes > 0 ORDER BY fraudes DESC, importe_fraude DESC LIMIT 10
    ''', desde=desde)
    return {'por_tipo': por_tipo, 'por_hora': por_hora, 'por_dia_semana': por_dia_semana,
            'por_canal': tasa_por('canal_tipo', desde),
            'por_categoria': tasa_por('categoria_negocio', desde),
            'por_dispositivo': tasa_por('dispositivo_tipo', desde, nulo='presencial'),
            'por_pais_ip': tasa_por('ip_pais', desde, limite=8, nulo='presencial'),
            'senales': senales, 'top_comercios': top_comercios, 'top_clientes': top_clientes}


# ---------------------------------------------------------------------------- transacciones
FILTROS = {
    'todas': '1',
    'fraude': 'te.es_fraude = 1',
    'alerta': 'notEmpty(r.reglas)',
    'anomala': f's.score_pct >= {UMBRAL_ANOMALIA}',
    'revisada': "f.veredicto != ''",
}


@app.get('/api/transacciones')
def transacciones(filtro: str = Query('todas', enum=list(FILTROS)), limite: int = Limite):
    """Últimas transacciones con su score del detector, las reglas que dispararon y el veredicto."""
    if filtro not in FILTROS:      # enum= solo lo documenta en /api/docs, no lo comprueba
        raise HTTPException(422, f'Filtro desconocido: {filtro}. Opciones: {", ".join(FILTROS)}')
    return filas(f'''
        SELECT te.transaccion_id AS transaccion_id, te.timestamp AS timestamp, te.cliente_id AS cliente_id,
               te.comercio_nombre AS comercio, te.categoria_negocio AS categoria, te.canal_tipo AS canal,
               toFloat64(te.cantidad) AS importe, te.estado AS estado, te.es_fraude AS es_fraude,
               te.tipo_fraude AS tipo_fraude, s.score_pct AS score_pct, r.reglas AS reglas,
               f.veredicto AS veredicto
        FROM transacciones_enriquecidas AS te
        LEFT JOIN (SELECT transaccion_id, score_pct FROM anomaly_scores WHERE model = {{m:String}}) AS s
               ON s.transaccion_id = te.transaccion_id
        LEFT JOIN (SELECT a.transaccion_id AS transaccion_id, groupArray(p.nombre) AS reglas
                   FROM alerta AS a INNER JOIN patron AS p ON a.patron_id = p.patron_id
                   WHERE a.origen = 'regla' GROUP BY a.transaccion_id) AS r
               ON r.transaccion_id = te.transaccion_id
        LEFT JOIN (SELECT transaccion_id, any(veredicto) AS veredicto FROM feedback_analistas
                   GROUP BY transaccion_id) AS f
               ON f.transaccion_id = te.transaccion_id
        WHERE {FILTROS[filtro]}
        ORDER BY te.timestamp DESC, te.transaccion_id DESC
        LIMIT {{limite:UInt32}}
    ''', m=MODELO_BASE, limite=limite)


# ---------------------------------------------------------------------- alertas y analistas
@app.get('/api/alertas')
def alertas(dias: int = Dias, limite: int = Limite):
    """Alertas de las reglas: cuántas por regla (y cuántas eran fraude) y las últimas."""
    desde, _ = periodo(dias)
    por_regla = filas('''
        SELECT p.nombre AS regla, count() AS alertas, countIf(t.es_fraude = 1) AS fraudes,
               fraudes / alertas AS precision
        FROM alerta AS a INNER JOIN patron AS p ON a.patron_id = p.patron_id
                         INNER JOIN transaccion AS t ON a.transaccion_id = t.transaccion_id
        WHERE a.origen = 'regla' AND t.timestamp >= {desde:DateTime}
        GROUP BY regla ORDER BY alertas DESC
    ''', desde=desde)
    ultimas = filas('''
        SELECT a.alerta_id AS alerta_id, a.fecha AS fecha, a.transaccion_id AS transaccion_id,
               p.nombre AS regla, a.nota_riesgo AS nota_riesgo, a.estado AS estado,
               toFloat64(te.cantidad) AS importe, te.comercio_nombre AS comercio, te.cliente_id AS cliente_id,
               te.es_fraude AS es_fraude, te.tipo_fraude AS tipo_fraude
        FROM alerta AS a INNER JOIN patron AS p ON a.patron_id = p.patron_id
                         INNER JOIN transacciones_enriquecidas AS te ON a.transaccion_id = te.transaccion_id
        WHERE a.origen = 'regla'
        ORDER BY a.fecha DESC, a.alerta_id DESC LIMIT {limite:UInt32}
    ''', limite=limite)
    return {'por_regla': por_regla, 'ultimas': ultimas}


@app.get('/api/analistas')
def analistas(limite: int = Limite):
    """Revisiones de los analistas: las últimas, el acierto de cada analista y la evolución semanal."""
    ultimas = filas('''
        SELECT f.fecha_revision AS fecha_revision, f.fecha_alerta AS fecha_alerta,
               f.transaccion_id AS transaccion_id, an.nombre AS analista, an.nivel AS nivel,
               f.veredicto AS veredicto, f.comentario AS comentario, f.modelo AS modelo,
               f.puntuacion AS puntuacion, t.es_fraude AS es_fraude, t.tipo_fraude AS tipo_fraude,
               toFloat64(t.cantidad) AS importe
        FROM feedback_analistas AS f INNER JOIN analista AS an ON f.analista_id = an.analista_id
                                     INNER JOIN transaccion AS t ON f.transaccion_id = t.transaccion_id
        ORDER BY f.fecha_revision DESC LIMIT {limite:UInt32}
    ''', limite=limite)
    por_analista = filas('''
        SELECT an.nombre AS analista, an.nivel AS nivel, count() AS revisadas,
               countIf((f.veredicto = 'fraude') = (t.es_fraude = 1)) AS aciertos,
               countIf(f.veredicto = 'fraude') AS marcadas_fraude, aciertos / revisadas AS acierto
        FROM feedback_analistas AS f INNER JOIN analista AS an ON f.analista_id = an.analista_id
                                     INNER JOIN transaccion AS t ON f.transaccion_id = t.transaccion_id
        GROUP BY analista, nivel ORDER BY revisadas DESC
    ''')
    return {'ultimas': ultimas, 'por_analista': por_analista, 'semanal': csv('feedback_semanal.csv')}


# -------------------------------------------------------------------------------- anomalías
@app.get('/api/anomalias')
def anomalias(modelo: str = Query(MODELO_BASE), dias: int = Dias, limite: int = Limite):
    """Distribución del score de un detector (fraude frente a legítimas) y las más anómalas."""
    desde, _ = periodo(dias)
    modelos = [f['model'] for f in filas('SELECT DISTINCT model FROM anomaly_scores ORDER BY model')]
    if modelo not in modelos:
        raise HTTPException(404, f'Modelo desconocido: {modelo}. Disponibles: {", ".join(modelos)}')
    histograma = filas('''
        SELECT least(floor(s.score_pct * 20), 19) / 20 AS tramo, count() AS transacciones,
               countIf(t.es_fraude = 1) AS fraudes
        FROM anomaly_scores AS s INNER JOIN transaccion AS t ON s.transaccion_id = t.transaccion_id
        WHERE s.model = {m:String} AND t.timestamp >= {desde:DateTime}
        GROUP BY tramo ORDER BY tramo
    ''', m=modelo, desde=desde)
    resumen = filas('''
        SELECT countIf(s.score_pct >= {u:Float64}) AS anomalas,
               countIf(s.score_pct >= {u:Float64} AND t.es_fraude = 1) AS anomalas_fraude,
               countIf(t.es_fraude = 1) AS fraudes, count() AS transacciones
        FROM anomaly_scores AS s INNER JOIN transaccion AS t ON s.transaccion_id = t.transaccion_id
        WHERE s.model = {m:String} AND t.timestamp >= {desde:DateTime}
    ''', m=modelo, u=UMBRAL_ANOMALIA, desde=desde)[0]
    top = filas('''
        SELECT s.transaccion_id AS transaccion_id, te.timestamp AS timestamp, te.cliente_id AS cliente_id,
               te.comercio_nombre AS comercio, te.canal_tipo AS canal, toFloat64(te.cantidad) AS importe,
               s.score AS score, s.score_pct AS score_pct, te.es_fraude AS es_fraude, te.tipo_fraude AS tipo_fraude
        FROM anomaly_scores AS s INNER JOIN transacciones_enriquecidas AS te ON s.transaccion_id = te.transaccion_id
        WHERE s.model = {m:String} AND te.timestamp >= {desde:DateTime}
        ORDER BY s.score DESC LIMIT {limite:UInt32}
    ''', m=modelo, desde=desde, limite=limite)
    return {'modelo': modelo, 'modelos': modelos, 'umbral': UMBRAL_ANOMALIA, 'resumen': resumen,
            'histograma': histograma, 'top': top}


# ------------------------------------------------------------------------- modelos y ajuste
@app.get('/api/modelos')
def modelos():
    """Comparativa de los 5 detectores en test, recall por tipo y ajuste de umbrales (Hito 6)."""
    fb = CONFIG['feedback']
    return {'comparativa': csv('comparativa_test.csv'), 'recall_por_tipo': csv('recall_por_tipo_test.csv'),
            'ajuste_reglas': csv('ajuste_reglas.csv'),
            'ajuste_config': {'umbral_minimo': fb.get('umbral_minimo'),
                              'variables_extra': fb.get('variables_extra') or [],
                              'variables_quitadas': fb.get('variables_quitadas') or []}}
