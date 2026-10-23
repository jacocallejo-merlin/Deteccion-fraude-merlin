"""
Web de los modelos: un informe visual, para dirección, de los modelos que hemos entrenado, cuál usamos y por qué.

    uvicorn web_modelos.app:app --port 8001 --reload   # desde la raíz del proyecto -> http://localhost:8001

Solo para el administrador de la entidad (es el informe técnico, no una herramienta del día a día): usa
la sesión de la web de casos (web/sesiones.py, con la base de datos de cuentas de web/bd_cuentas.py; el
navegador manda la misma cookie a los dos puertos) y, sin ella, manda a iniciar sesión allí. Los datos los
lee de lo que deja el pipeline en disco. Las notas de cada modelo en el examen final
(modelos/*_scores_test.parquet), la respuesta real de cada pago (datos_ml/info_test.parquet), cómo se
entrenaron (modelos/resumen_entrenamiento.json, autoencoder_resumen.json) y las comparativas de resultados/.

Publicada en internet (despliegue/publicar.py) no lee esos archivos: sirve publicado.json, el mismo resultado
calculado al publicar, y así el contenedor no necesita pandas.

Todos los modelos se comparan en igualdad de condiciones: cada uno puede avisar del mismo número de pagos
que el equipo revisa al día (~1 % de los pagos, el "presupuesto" de la comparativa).
"""
import json
import os
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from config import CONFIG
from web.sesiones import Almacen, ProtegerWeb

ESTATICOS = Path(__file__).resolve().parent / 'static'
MODELOS = RAIZ / 'modelos'
RESULTADOS = RAIZ / 'resultados'
INFO_TEST = RAIZ / 'datos_ml' / 'info_test.parquet'
PUBLICADO = Path(__file__).resolve().parent / 'publicado.json'   # solo existe en el contenedor publicado

# Cada modelo en lenguaje claro: la idea en una frase
DETECTORES = {
    'kmeans': {
        'nombre': 'K-Means', 'familia': 'Agrupamiento',
        'idea': 'Agrupa los pagos parecidos; avisa del que queda lejos de todos los grupos.',
        'como_funciona': 'Busca unos pocos grupos donde caben casi todos los pagos normales: por ejemplo, compras '
                         'pequeñas de cada día o pagos grandes de clientes habituales. Cada pago nuevo se compara '
                         'con el grupo más cercano, y cuanto más lejos queda de él, más raro es.',
    },
    'ocsvm': {
        'nombre': 'One-Class SVM', 'familia': 'Frontera',
        'idea': 'Traza una frontera alrededor de lo normal; avisa de lo que cae fuera.',
        'como_funciona': 'Aprende la forma de la nube que forman los pagos normales y traza una frontera a su '
                         'alrededor. Un pago que cae fuera de la frontera, o muy cerca del borde, se considera raro.',
    },
    'dbscan': {
        'nombre': 'DBSCAN', 'familia': 'Densidad',
        'idea': 'Mira si cada pago tiene vecinos parecidos; avisa del que está solo.',
        'como_funciona': 'Une los pagos que tienen muchos vecinos parecidos en zonas densas. Un pago que cae en una '
                         'zona vacía, sin vecinos cerca, no pertenece a ningún grupo y se considera raro.',
    },
    'iforest': {
        'nombre': 'Isolation Forest', 'familia': 'Aislamiento',
        'idea': 'Parte los pagos al azar; avisa del que se queda solo enseguida.',
        'como_funciona': 'Hace cortes al azar en los datos, como un juego de preguntas de sí o no. Un pago normal '
                         'necesita muchas preguntas para quedarse solo; uno raro queda aislado con muy pocas.',
    },
    'autoencoder': {
        'nombre': 'Autoencoder', 'familia': 'Red neuronal',
        'idea': 'Una red neuronal que copia pagos normales; avisa del que no sabe copiar.',
        'como_funciona': 'Una red neuronal aprende a resumir cada pago en unos pocos números y a reconstruirlo '
                         'después. Con los pagos normales lo hace bien; si no consigue reconstruir uno, es que es raro.',
    },
}

TIPOS_FRAUDE = {
    'account_takeover': 'Cuenta robada',
    'card_testing': 'Pruebas con tarjetas robadas',
    'dispositivo_nuevo': 'Dispositivo desconocido',
    'bust_out': 'Gastarlo todo de golpe',
    'bot': 'Compras de robots',
    'smurfing': 'Muchos pagos pequeños',
    'ip_sospechosa': 'Conexión sospechosa',
    'pico_gasto': 'Pico de gasto',
    'devolucion_abusiva': 'Devoluciones abusivas',
    'cash_out_multicomercio': 'Vaciar la tarjeta en muchas tiendas',
}
FRAUDE_NUEVO = 'cash_out_multicomercio'   # aparece solo en el examen: ningún modelo lo había visto al entrenar

app = FastAPI(title='Centinela · Modelos', docs_url='/api/docs', redoc_url=None)
app.mount('/static', StaticFiles(directory=ESTATICOS), name='static')


def web_casos(request):
    """Dónde está la web de casos: WEB_CASOS_URL (publicada, "/": la misma dirección) o, si no, la misma
    máquina en el puerto 8000."""
    if os.environ.get('WEB_CASOS_URL') is not None:
        return os.environ['WEB_CASOS_URL'].rstrip('/')
    return f'{request.url.scheme}://{request.url.hostname}:8000'


_almacen = Almacen()
app.add_middleware(ProtegerWeb, almacen=lambda: _almacen, solo_roles=('administrador',),
                   login=lambda request: f'{web_casos(request)}/login')

_cache = {'clave': None, 'datos': None}


def _archivos():
    return ([INFO_TEST, RESULTADOS / 'comparativa_test.csv', RESULTADOS / 'recall_por_tipo_test.csv',
             MODELOS / 'resumen_entrenamiento.json']
            + [MODELOS / f'{m}_scores_test.parquet' for m in DETECTORES])


def _leer_json(ruta):
    return json.loads(ruta.read_text(encoding='utf-8')) if ruta.exists() else {}


def _calcular():
    import numpy as np    # aquí y no arriba: la web publicada no los carga (ver PUBLICADO)
    import pandas as pd

    info = pd.read_parquet(INFO_TEST)
    comparativa = pd.read_csv(RESULTADOS / 'comparativa_test.csv').set_index('detector')
    y = info['es_fraude'].to_numpy().astype(bool)
    n = len(info)
    dias = int(info['timestamp'].dt.date.nunique())

    modelos = []
    for clave, textos in DETECTORES.items():
        if clave not in comparativa.index:
            continue
        nota = pd.read_parquet(MODELOS / f'{clave}_scores_test.parquet')['score'].to_numpy()
        fila = comparativa.loc[clave]
        avisos = int(round(fila['pres_marcadas_%'] * n / 100))        # cuántos pagos puede señalar
        marcados = np.argsort(-nota, kind='stable')[:avisos]
        modelos.append({
            'clave': clave, **textos, 'elegido': str(fila.get('elegido', '')).strip() == '<--',
            'avisos': avisos, 'avisos_dia': round(avisos / dias, 1), 'encontrados': int(y[marcados].sum()),
            'veces_azar': round(float(fila['lift']), 1),                # cuántas veces mejor que elegir al azar
            'nota_validacion': float(fila['PR-AUC_val']),                # la prueba previa con la que se eligió
        })
    clasicos = _leer_json(MODELOS / 'resumen_entrenamiento.json')
    red = _leer_json(MODELOS / 'autoencoder_resumen.json')
    historico = RESULTADOS / 'historico_autoencoders.csv'
    for m in modelos:
        if m['clave'] == 'autoencoder':
            ent = red.get('entrenamiento', {})
            m['entrenamiento'] = {
                'versiones': len(pd.read_csv(historico)) if historico.exists() else 1,
                'parametros': {'arquitectura': red.get('arquitectura'),
                               'activacion': red.get('activacion'),
                               'seleccion': red.get('seleccion'),
                               'cuello': red.get('mejores_parametros', {}).get('cuello'),
                               'rondas': ent.get('epocas'), 'mejor_ronda': ent.get('mejor_epoca')},
            }
        else:
            c = clasicos.get(m['clave'], {})
            m['entrenamiento'] = {'versiones': int(c.get('combinaciones_probadas', 0)),
                                  'parametros': c.get('mejores_parametros', {})}
    modelos.sort(key=lambda m: (-m['encontrados'], not m['elegido'], -m['veces_azar']))
    mejor_validacion = max(modelos, key=lambda m: m['nota_validacion'])['clave']

    por_tipo = pd.read_csv(RESULTADOS / 'recall_por_tipo_test.csv', index_col=0)
    tipos = [{'clave': t, 'nombre': TIPOS_FRAUDE.get(t, t.replace('_', ' ').capitalize()), 'casos': int(f['n']),
              'nuevo': t == FRAUDE_NUEVO,
              'encontrado': {m['clave']: float(f[m['clave']]) for m in modelos if m['clave'] in f}}
             for t, f in por_tipo.iterrows()]

    # Cómo se entrenaron: lo que se probó y con cuántos pagos
    uno = next(iter(clasicos.values()), {})
    proceso = {
        'dias_datos': CONFIG['dataset']['dias'],
        'pagos_aprender': int(uno.get('filas_ajuste', 0) + uno.get('filas_validacion', 0)),
        'versiones_clasicos': int(sum(v.get('combinaciones_probadas', 0) for v in clasicos.values())),
        'versiones_red': len(pd.read_csv(historico)) if historico.exists() else 1,
        # los parámetros con los que se quedó cada modelo clásico (p. ej. K-Means con k grupos)
        'parametros': {k: v.get('mejores_parametros', {}) for k, v in clasicos.items()},
    }

    semanas = []
    ruta = RESULTADOS / 'feedback_semanal.csv'
    if ruta.exists():
        sem = pd.read_csv(ruta)
        semanas = [{'semana': int(s['semana']), 'desde': s['desde'], 'completa': int(s['dias']) == 7,
                    'aprendiendo': round(float(s['precision']) * 100, 1),
                    'sin_aprender': round(float(s['precision_kmeans']) * 100, 1)}
                   for _, s in sem.iterrows()]

    return {
        'pagos': n, 'fraudes': int(y.sum()), 'dias': dias,
        'desde': str(info['timestamp'].min().date()), 'hasta': str(info['timestamp'].max().date()),
        'modelos': modelos, 'mejor_validacion': mejor_validacion, 'tipos': tipos, 'proceso': proceso,
        'semanas': semanas,
    }


@app.get('/', include_in_schema=False)
def pagina():
    return FileResponse(ESTATICOS / 'index.html', headers={'Cache-Control': 'no-cache'})


@app.get('/api/modelos')
def modelos():
    """Todo lo que enseña la página. Se recalcula solo si el pipeline ha cambiado algún archivo."""
    if PUBLICADO.exists():
        if _cache['clave'] != 'publicado':
            _cache['datos'], _cache['clave'] = json.loads(PUBLICADO.read_text(encoding='utf-8')), 'publicado'
        return {**_cache['datos'], 'web_casos': os.getenv('WEB_CASOS_URL')}
    faltan = [str(a.relative_to(RAIZ)) for a in _archivos() if not a.exists()]
    if faltan:
        raise HTTPException(503, 'Faltan los resultados de los modelos (' + ', '.join(faltan[:3])
                            + '). Ejecuta antes el pipeline: python main.py')
    clave = tuple(a.stat().st_mtime for a in _archivos())
    if _cache['clave'] != clave:
        try:
            _cache['datos'] = _calcular()
        except Exception as e:
            raise HTTPException(503, f'No se han podido leer los resultados de los modelos: {str(e)[:200]}')
        _cache['clave'] = clave
    # la web de casos: la que diga WEB_CASOS_URL (despliegue con una sola dirección) o, si no, el puerto 8000
    return {**_cache['datos'], 'web_casos': os.getenv('WEB_CASOS_URL')}
