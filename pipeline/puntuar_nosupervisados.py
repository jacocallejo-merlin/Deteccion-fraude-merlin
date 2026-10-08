import argparse
import sys
from datetime import datetime
from pathlib import Path

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_MODELOS, HOST, PORT, USER, PASSWORD, DATABASE
from preparacion_datos_ml import preparar_features
from modelos_nosupervisados import MODELOS, puntuar, percentil


def conectar():
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                           password=PASSWORD, database=DATABASE)
    if not client.command('EXISTS TABLE anomaly_scores'):
        raise SystemExit('No existe anomaly_scores. Lanza antes modelos_nosupervisados.py')
    return client


def cargar_artefacto(ruta):
    if not ruta.exists():
        raise SystemExit(f'Falta {ruta.name}. Lanza antes el entrenamiento.')
    return joblib.load(ruta)


def pendientes(client, nombre):
    """Transacciones que todavía no tienen score de este modelo."""
    return client.query_df('''
        SELECT * FROM features_transaccion
        WHERE transaccion_id NOT IN (
            SELECT transaccion_id FROM anomaly_scores WHERE model = {modelo:String})
        ORDER BY timestamp
    ''', parameters={'modelo': nombre})


def puntuar_modelo(client, nombre, escalador, run_id):
    modelo = cargar_artefacto(CARPETA_MODELOS / f'{nombre}.joblib')
    referencia = cargar_artefacto(CARPETA_MODELOS / f'{nombre}_referencia.joblib')

    df = pendientes(client, nombre)
    if df.empty:
        print(f'{nombre}: no hay transacciones pendientes')
        return 0

    X, info = preparar_features(df, escalador)       # transform, nunca fit
    s, _ = puntuar(nombre, modelo, X.to_numpy())

    tabla = pd.DataFrame({
        'transaccion_id': info['transaccion_id'].to_numpy(dtype=np.uint32),
        'model': nombre,
        'score': s.astype(np.float64),
        'score_pct': percentil(s, referencia).astype(np.float32),
        'run_id': run_id,
    })
    client.insert_df('anomaly_scores', tabla)

    raras = int((tabla['score_pct'] >= 0.99).sum())
    print(f'{nombre}: {len(tabla):,} transacciones puntuadas '
          f'({raras:,} más raras que el 99 % del ajuste)')
    return len(tabla)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('modelos', nargs='*', default='todos', choices=MODELOS + ['todos'])
    args = parser.parse_args()
    elegidos = MODELOS if 'todos' in args.modelos else list(dict.fromkeys(args.modelos))

    run_id = f'scoring_{datetime.now():%Y%m%d_%H%M%S}'
    print(f'run_id: {run_id}')
    client = conectar()
    escalador = cargar_artefacto(CARPETA_DATOS / 'escalador.joblib')

    total = sum(puntuar_modelo(client, n, escalador, run_id) for n in elegidos)
    print(f'\nTotal: {total:,} scores nuevos en anomaly_scores')


if __name__ == '__main__':
    main()