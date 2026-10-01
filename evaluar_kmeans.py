"""
Evaluación de un modelo de detección de anomalías, usando es_fraude SOLO para medir.

No entrena nada: lee los scores que ya guardó el script de entrenamiento. Así se
evalúa exactamente el modelo que está en disco, y no una copia reentrenada que
podría haberse quedado con parámetros distintos.

Sirve para cualquier modelo del proyecto (KMeans, Isolation Forest, One-Class SVM).
El contrato es: el modelo deja en modelos/ dos ficheros

    <MODELO>_scores_train.parquet
    <MODELO>_scores_test.parquet

con al menos una columna de score continua donde MÁS ALTO = MÁS ANÓMALO, y
opcionalmente una columna 'cluster'. Cambiando MODELO y COLUMNA_SCORE, los tres
modelos del equipo se miden con el mismo código y las cifras son comparables.
"""

import os

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, roc_auc_score

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'

MODELO = 'kmeans'          # prefijo de los ficheros de scores
COLUMNA_SCORE = 'distancia'  # columna continua, mayor = más anómalo
PORCENTAJES = [0.5, 1, 2, 5, 10]   # % de transacciones más raras que se revisarían
TOP_DETALLE = 1            # sobre qué corte se desglosan los tipos de fraude


def cargar():
    datos = {}
    for parte in ['train', 'test']:
        scores = pd.read_parquet(
            os.path.join(CARPETA_MODELOS, f'{MODELO}_scores_{parte}.parquet'))
        info = pd.read_parquet(os.path.join(CARPETA_DATOS, f'info_{parte}.parquet'))

        columna = COLUMNA_SCORE if COLUMNA_SCORE in scores.columns else 'score'
        if columna not in scores.columns:
            raise KeyError(f'{MODELO}_scores_{parte}.parquet no tiene ninguna columna '
                           f'de score ({COLUMNA_SCORE!r} ni "score")')

        datos[parte] = {
            'score': scores[columna].to_numpy(dtype=float),
            'cluster': scores['cluster'].to_numpy() if 'cluster' in scores.columns else None,
            'y': info['es_fraude'].astype(int).to_numpy(),
            'tipo': info['tipo_fraude'].to_numpy() if 'tipo_fraude' in info.columns else None,
        }
        if len(datos[parte]['score']) != len(datos[parte]['y']):
            raise ValueError(f'{parte}: {len(datos[parte]["score"])} scores frente a '
                             f'{len(datos[parte]["y"])} filas en info_{parte}.parquet')
    print(f'Modelo "{MODELO}", columna de score "{columna}"')
    return datos


def resumen_global(datos):
    print(f'\nEn test hay {datos["test"]["y"].sum()} fraudes de '
          f'{len(datos["test"]["y"]):,} ({datos["test"]["y"].mean() * 100:.2f} %).')
    print('  Por eso no se reporta accuracy: un modelo que dijera "nada es fraude" '
          f'tendría {(1 - datos["test"]["y"].mean()) * 100:.2f} % sin detectar ninguno.')

    filas = []
    for parte, d in datos.items():
        y, s = d['y'], d['score']
        pr = average_precision_score(y, s)
        filas.append({
            'conjunto': parte,
            'n': len(y),
            'fraudes': int(y.sum()),
            'base_rate_%': round(y.mean() * 100, 2),
            'PR-AUC': round(pr, 3),
            'ROC-AUC': round(roc_auc_score(y, s), 3),
            'PR-AUC/base': round(pr / y.mean(), 1),
        })
    print('\nEl score como detector de fraude:')
    print(pd.DataFrame(filas).to_string(index=False))
    print('  PR-AUC: el suelo es el base rate (score aleatorio), 1.0 sería perfecto.')
    print('  ROC-AUC: 0.5 es azar; con clases muy desbalanceadas sale optimista,')
    print('           así que la métrica que manda es la PR-AUC.')
    print('  PR-AUC/base: veces mejor que el azar. Como no depende del base rate,')
    print('               es lo que permite comparar train y test de forma justa.')


def tabla_top_k(y, score):
    """Si un analista revisara solo el top k% más raro, ¿cuánto fraude pillaría?
    El techo es el propio presupuesto: con n plazas no se pueden pillar más de n."""
    orden = np.argsort(-score)
    filas = []
    for pct in PORCENTAJES:
        n = max(1, int(round(len(score) * pct / 100)))
        pillados = int(y[orden[:n]].sum())
        precision = pillados / n
        filas.append({
            'top_%': pct,
            'revisadas': n,
            'fraudes_pillados': pillados,
            'precision_%': round(precision * 100, 1),
            'recall_%': round(pillados / max(1, y.sum()) * 100, 1),
            'techo_recall_%': round(min(n, y.sum()) / max(1, y.sum()) * 100, 1),
            'lift': round(precision / y.mean(), 1),
        })
    return pd.DataFrame(filas)


def por_cluster(d):
    tabla = (pd.DataFrame({'cluster': d['cluster'], 'es_fraude': d['y']})
             .groupby('cluster')['es_fraude']
             .agg(n='size', fraudes='sum')
             .assign(tasa_fraude_pct=lambda t: (t['fraudes'] / t['n'] * 100).round(2),
                     score_mediano=lambda t: [round(float(np.median(d['score'][d['cluster'] == c])), 2)
                                              for c in t.index]))
    print('\nTasa de fraude por cluster (test):')
    print(tabla.to_string())
    print('  Lo que detecta fraude es el score, no el cluster en el que cae la transacción.')


def tipos_detectados(d):
    y, score, tipos = d['y'], d['score'], d['tipo']
    n = max(1, int(round(len(score) * TOP_DETALLE / 100)))
    top = np.argsort(-score)[:n]
    total = pd.Series(tipos[y == 1]).value_counts()
    pillados = pd.Series(tipos[top][y[top] == 1]).value_counts()
    detalle = (pd.DataFrame({'fraudes_en_test': total,
                             f'pillados_top{TOP_DETALLE}%': pillados})
               .fillna(0).astype(int))
    detalle['recall_%'] = (detalle[f'pillados_top{TOP_DETALLE}%'] /
                           detalle['fraudes_en_test'] * 100).round(1)
    print(f'\nTipos de fraude que pilla el top {TOP_DETALLE} % ({n} transacciones):')
    print(detalle.sort_values('recall_%', ascending=False).to_string())
    print('  Los tipos con recall bajo son los que este modelo no ve: ahí es donde')
    print('  aportan las reglas o un modelo distinto.')


def main():
    datos = cargar()
    print('\n' + '=' * 78)
    print(f'EVALUACIÓN DE "{MODELO}" (es_fraude, solo para medir)')
    print('=' * 78)

    resumen_global(datos)

    prueba = datos['test']
    print('\nSi se revisara solo el top k% más raro del test:')
    print(tabla_top_k(prueba['y'], prueba['score']).to_string(index=False))
    print('  lift = cuántas veces más fraude encuentras que revisando al azar')
    print('  techo_recall_% = lo máximo alcanzable con ese nº de plazas')

    if prueba['cluster'] is not None:
        por_cluster(prueba)
    if prueba['tipo'] is not None:
        tipos_detectados(prueba)


if __name__ == '__main__':
    main()