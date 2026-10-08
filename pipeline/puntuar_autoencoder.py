"""
Puntúa con el autoencoder ya entrenado las transacciones de features_transaccion
que aún no tienen score suyo, y las guarda en anomaly_scores (model = 'autoencoder').

No entrena nada: usa autoencoder.pt y autoencoder_referencia.joblib, que deja
entrenar_autoencoder.py. Es el paso del DAG de scoring para el autoencoder.
Se puede ejecutar las veces que haga falta: solo puntúa lo pendiente, nunca duplica.

  score      = error de reconstrucción (más alto = más anómalo)
  score_pct  = fracción de transacciones de la referencia del entrenamiento con
               un error menor o igual (0.99 = más rara que el 99 %)

Uso:  python pipeline/puntuar_autoencoder.py
"""
import sys
from datetime import datetime
from pathlib import Path

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd
import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_MODELOS, HOST, PORT, USER, PASSWORD, DATABASE
from preparacion_datos_ml import preparar_features
from modelos_nosupervisados import percentil
from entrenar_autoencoder import Autoencoder, error_reconstruccion

MODELO = 'autoencoder'


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


def cargar_autoencoder():
    """Reconstruye la red con la arquitectura guardada y le carga los pesos."""
    g = torch.load(comprobar_existe(CARPETA_MODELOS / f'{MODELO}.pt'), weights_only=False)
    modelo = Autoencoder(g['n_entrada'], g['capas'], g['cuello'])
    modelo.load_state_dict(g['estado'])
    modelo.eval()
    return modelo, g['columnas']


def pendientes(client):
    """Transacciones que todavía no tienen score del autoencoder."""
    return client.query_df('''
        SELECT * FROM features_transaccion
        WHERE transaccion_id NOT IN (
            SELECT transaccion_id FROM anomaly_scores WHERE model = {modelo:String})
        ORDER BY timestamp
    ''', parameters={'modelo': MODELO})


def main():
    run_id = f'scoring_{datetime.now():%Y%m%d_%H%M%S}'
    print(f'run_id: {run_id}')

    client = conectar()
    escalador = joblib.load(comprobar_existe(CARPETA_DATOS / 'escalador.joblib'))
    referencia = joblib.load(comprobar_existe(CARPETA_MODELOS / f'{MODELO}_referencia.joblib'))
    modelo, columnas = cargar_autoencoder()

    df = pendientes(client)
    if df.empty:
        print(f'{MODELO}: no hay transacciones pendientes')
        return

    X, info = preparar_features(df, escalador)       # transform, nunca fit
    X = X[columnas]                                    # mismo orden que en el entrenamiento
    error = error_reconstruccion(modelo, X)

    tabla = pd.DataFrame({
        'transaccion_id': info['transaccion_id'].to_numpy(dtype=np.uint32),
        'model': MODELO,
        'score': error.astype(np.float64),
        'score_pct': percentil(error, referencia).astype(np.float32),
        'run_id': run_id,
    })
    client.insert_df('anomaly_scores', tabla)

    raras = int((tabla['score_pct'] >= 0.99).sum())
    print(f'{MODELO}: {len(tabla):,} transacciones puntuadas '
          f'({raras:,} más raras que el 99 % de la referencia)')


if __name__ == '__main__':
    main()