"""
Puntúa con el modelo supervisado HGB + GMM las transacciones de features_transaccion
que aún no tienen score suyo, y las guarda en anomaly_scores (model = 'hgb_gmm').

No entrena nada: usa hgb_gmm.joblib y hgb_gmm_referencia.joblib, que deja
entrenar_modelo_supervisado.py. Es el paso del DAG de scoring para el modelo base.
Se puede ejecutar las veces que haga falta: puntúa solo lo pendiente y nunca duplica.
Si el modelo guardado es de otro entrenamiento, borra los scores antiguos y los rehace
todos: así el circuito de alertas no mezcla dos versiones del modelo.

  score      = probabilidad de fraude del HGB (más alto = más sospechoso)
  score_pct  = fracción de transacciones de la referencia del entrenamiento con
               un score menor o igual (0.99 = más rara que el 99 %)

Uso:  python pipeline/puntuar_modelo_supervisado.py
"""
import json
import sys
from pathlib import Path

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_MODELOS, DATABASE, HOST, PASSWORD, PORT, USER
from modelo_supervisado import cargar, puntuar
from modelos_nosupervisados import percentil
from preparacion_datos_ml import preparar_features

MODELO = 'hgb_gmm'
VARIABLE_EXTRA = 'n_comercios_cliente_3h'


def conectar():
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                           password=PASSWORD, database=DATABASE)
    if not client.command('EXISTS TABLE anomaly_scores'):
        raise SystemExit('No existe anomaly_scores. Lanza antes modelos_nosupervisados.py')
    return client


def comprobar_existe(ruta):
    if not ruta.exists():
        raise SystemExit(f'Falta {ruta.name}. Lanza antes el entrenamiento.')
    return ruta


def run_id_entrenamiento():
    """run_id del modelo guardado: identifica qué versión han producido los scores."""
    ruta = comprobar_existe(CARPETA_MODELOS / f'{MODELO}_resumen.json')
    with open(ruta, encoding='utf-8') as fh:
        return json.load(fh)['run_id']


def estado_scores(client, run_id):
    """(filas de esta versión, filas de versiones anteriores) en anomaly_scores."""
    fila = client.query('''
        SELECT countIf(run_id = {run_id:String}),
               countIf(run_id != {run_id:String})
        FROM anomaly_scores WHERE model = {modelo:String}
    ''', parameters={'run_id': run_id, 'modelo': MODELO}).result_rows[0]
    return int(fila[0]), int(fila[1])


def rehacer(client):
    """Borra los scores de versiones antiguas y devuelve todas las transacciones."""
    client.command('ALTER TABLE anomaly_scores DELETE WHERE model = {modelo:String} '
                   'SETTINGS mutations_sync = 2', parameters={'modelo': MODELO})
    return client.query_df('SELECT * FROM features_transaccion ORDER BY timestamp')


def pendientes(client):
    """Transacciones que todavía no tienen score de este modelo."""
    return client.query_df('''
        SELECT * FROM features_transaccion
        WHERE transaccion_id NOT IN (
            SELECT transaccion_id FROM anomaly_scores WHERE model = {modelo:String})
        ORDER BY timestamp
    ''', parameters={'modelo': MODELO})


def main():
    run_id = run_id_entrenamiento()
    print(f'run_id del modelo: {run_id}')

    client = conectar()
    escalador = joblib.load(comprobar_existe(CARPETA_DATOS / 'escalador.joblib'))
    referencia = joblib.load(comprobar_existe(CARPETA_MODELOS / f'{MODELO}_referencia.joblib'))
    modelo = cargar(comprobar_existe(CARPETA_MODELOS / f'{MODELO}.joblib'))

    actuales, antiguos = estado_scores(client, run_id)
    if antiguos:
        print(f'{MODELO}: {antiguos:,} scores de otra version; se rehacen todos')
    df = rehacer(client) if antiguos or not actuales else pendientes(client)
    if df.empty:
        print(f'{MODELO}: no hay transacciones pendientes')
        return
    if VARIABLE_EXTRA not in df.columns:
        raise SystemExit(f'Falta {VARIABLE_EXTRA} en features_transaccion. '
                         'Vuelve a ejecutar pipeline/features.py.')

    X, info = preparar_features(df, escalador)       # transform, nunca fit
    extra = df[VARIABLE_EXTRA].to_numpy(dtype=float)
    score, _ = puntuar(modelo, X, extra)

    tabla = pd.DataFrame({
        'transaccion_id': info['transaccion_id'].to_numpy(dtype=np.uint32),
        'model': MODELO,
        'score': score.astype(np.float64),
        'score_pct': percentil(score, referencia).astype(np.float32),
        'run_id': run_id,
    })
    client.insert_df('anomaly_scores', tabla)

    raras = int((tabla['score_pct'] >= 0.99).sum())
    print(f'{MODELO}: {len(tabla):,} transacciones puntuadas '
          f'({raras:,} más raras que el 99 % de la referencia)')


if __name__ == '__main__':
    main()
