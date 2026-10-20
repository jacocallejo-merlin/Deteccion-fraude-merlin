"""
Vigilancia del modelo (Hito 5): ¿los pagos recientes se parecen a aquellos con los que se entrenó?

Un detector de anomalías aprende "lo normal" del periodo de entrenamiento. Si los pagos cambian
(otro tipo de clientes, un fallo en los datos de origen, una variable que deja de llegar...), su
nota deja de tener sentido sin que nada falle. Este script lo detecta comparando, para cada variable
del modelo y para la nota del detector elegido, la distribución de los últimos días con la del
entrenamiento mediante el PSI (índice de estabilidad de la población):

    PSI < 0,10 estable · 0,10-0,25 vigilar · > 0,25 desviada (reentrenar o revisar los datos)

Además comprueba el volumen: si el último día llega con muchos menos pagos de lo normal, lo más
probable es que la carga haya fallado a medias.

Guarda el resultado en la tabla vigilancia_drift y en resultados/vigilancia_drift.csv, y si hay
algo desviado avisa por Teams/Slack/correo (notificar.py). No falla por encontrar drift: es un
aviso, no un error del pipeline. Se puede repetir sin duplicar (una fila por día y variable).
"""
import sys
from datetime import timedelta
from pathlib import Path

import clickhouse_connect
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_RESULTADOS, CONFIG, DATABASE, HOST, PASSWORD, PORT, USER
from notificar import notificar

_cfg = CONFIG['vigilancia']
MODELO_BASE = CONFIG['feedback']['modelo_base']
TRAMOS = 10                       # deciles del periodo de entrenamiento
MINIMO = 1e-4                     # proporción mínima de un tramo (evita log(0) en el PSI)


def psi(referencia, reciente):
    """PSI entre dos muestras de una variable. Los tramos son los deciles de la referencia (en una
    variable binaria quedan 2 tramos) y los nulos cuentan como un tramo más."""
    referencia, reciente = pd.Series(referencia), pd.Series(reciente)
    r = referencia.dropna().astype(float).to_numpy()
    n = reciente.dropna().astype(float).to_numpy()
    if not len(r) or not len(reciente):
        return float('nan')
    bordes = np.unique(np.quantile(r, np.linspace(0, 1, TRAMOS + 1)[1:-1]))
    def reparto(valores, total, nulos):
        conteo = np.bincount(np.searchsorted(bordes, valores, side='right'), minlength=len(bordes) + 1)
        return np.append(conteo / total, nulos)
    p = np.clip(reparto(r, len(referencia), referencia.isna().mean()), MINIMO, None)
    q = np.clip(reparto(n, len(reciente), reciente.isna().mean()), MINIMO, None)
    return float(np.sum((q - p) * np.log(q / p)))


def estado(valor):
    if np.isnan(valor):
        return 'sin datos'
    return 'desviada' if valor > _cfg['psi_alerta'] else 'vigilar' if valor > _cfg['psi_vigilar'] else 'estable'


def periodo_entrenamiento():
    """(desde, hasta) del train de preparacion_datos_ml.py (sin los días de calentamiento)."""
    desde = pd.read_parquet(CARPETA_DATOS / 'info_train.parquet', columns=['timestamp'])['timestamp'].min()
    hasta = pd.read_parquet(CARPETA_DATOS / 'info_test.parquet', columns=['timestamp'])['timestamp'].min()
    return desde, hasta


def leer(client, variables, desde, hasta):
    """Las variables del modelo y la nota del detector de los pagos entre desde y hasta."""
    columnas = ', '.join(f'f.{v} AS {v}' for v in variables)
    return client.query_df(f'''
        SELECT {columnas}, s.score_pct AS nota_{MODELO_BASE}
        FROM features_transaccion AS f
        LEFT JOIN (SELECT transaccion_id, score_pct FROM anomaly_scores WHERE model = {{m:String}}) AS s
            ON s.transaccion_id = f.transaccion_id
        WHERE f.timestamp >= {{desde:DateTime}} AND f.timestamp < {{hasta:DateTime}}
        SETTINGS join_use_nulls = 1
    ''', parameters={'m': MODELO_BASE, 'desde': desde, 'hasta': hasta})


def comprobar_volumen(client):
    """(día, pagos ese día, mediana de los 28 anteriores) del último día cargado."""
    dias = client.query_df('''
        SELECT toDate(timestamp) AS dia, count() AS pagos FROM features_transaccion
        WHERE dia >= (SELECT max(toDate(timestamp)) FROM features_transaccion) - 28
        GROUP BY dia ORDER BY dia''')
    if len(dias) < 2:
        return None
    return dias['dia'].iloc[-1], int(dias['pagos'].iloc[-1]), float(dias['pagos'].iloc[:-1].median())


def guardar(client, tabla):
    client.command('''
        CREATE TABLE IF NOT EXISTS vigilancia_drift (
            hasta       Date,                       -- último día de la ventana reciente
            desde       Date,
            variable    LowCardinality(String),
            psi         Float64,
            estado      LowCardinality(String),     -- estable, vigilar, desviada
            calculado   DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(calculado)
        ORDER BY (hasta, variable)
    ''')
    client.insert_df('vigilancia_drift', tabla[['hasta', 'desde', 'variable', 'psi', 'estado']])
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    tabla.to_csv(CARPETA_RESULTADOS / 'vigilancia_drift.csv', index=False)


def main():
    if not (CARPETA_DATOS / 'info_test.parquet').exists():
        print('No hay modelo entrenado (falta datos_ml/): nada que vigilar todavía')
        return
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                           database=DATABASE)
    # Las variables con las que se entrenó el modelo que existen tal cual en features_transaccion
    # (las transformadas, como hora_sin, salen de otras que ya se vigilan)
    del_modelo = set(pq.read_schema(CARPETA_DATOS / 'X_train.parquet').names)
    variables = [c for (c,) in client.query('SELECT name FROM system.columns WHERE database = {d:String} '
                                            "AND table = 'features_transaccion' ORDER BY position",
                                            parameters={'d': DATABASE}).result_rows if c in del_modelo]

    ref_desde, ref_hasta = periodo_entrenamiento()
    ultimo = client.command('SELECT max(toDate(timestamp)) FROM features_transaccion')
    ultimo = pd.Timestamp(ultimo).date()
    desde = ultimo - timedelta(days=_cfg['dias_recientes'] - 1)
    referencia = leer(client, variables, ref_desde, ref_hasta)
    reciente = leer(client, variables, desde, ultimo + timedelta(days=1))
    print(f'Entrenamiento: {ref_desde:%d/%m/%Y}-{ref_hasta:%d/%m/%Y} ({len(referencia):,} pagos). '
          f'Recientes: {desde:%d/%m/%Y}-{ultimo:%d/%m/%Y} ({len(reciente):,} pagos)')

    tabla = pd.DataFrame([{'variable': v, 'psi': psi(referencia[v], reciente[v])} for v in referencia.columns])
    tabla['estado'] = tabla['psi'].map(estado)
    tabla = tabla.assign(desde=desde, hasta=ultimo).sort_values('psi', ascending=False)
    guardar(client, tabla)

    print(f'\n{"variable":<30} {"PSI":>7}  estado')
    for f in tabla.itertuples():
        print(f'{f.variable:<30} {f.psi:>7.3f}  {f.estado}')
    resumen = tabla['estado'].value_counts()
    print('\n' + ', '.join(f'{resumen.get(e, 0)} {e}' for e in ('estable', 'vigilar', 'desviada')))

    problemas = [f'{f.variable}: PSI {f.psi:.2f}' for f in tabla.itertuples() if f.estado == 'desviada']
    volumen = comprobar_volumen(client)
    if volumen:
        dia, pagos, mediana = volumen
        print(f'Volumen: {pagos:,} pagos el {dia:%d/%m/%Y} (mediana de los 28 días anteriores: {mediana:,.0f})')
        if pagos < _cfg['caida_volumen'] * mediana:
            problemas.insert(0, f'Solo {pagos:,} pagos el {dia:%d/%m/%Y}, frente a {mediana:,.0f} de mediana: '
                                '¿la carga de datos ha fallado a medias?')
    if problemas:
        notificar(f'Vigilancia del modelo: {len(problemas)} problema{"s" if len(problemas) != 1 else ""}',
                  f'Los pagos del {desde:%d/%m} al {ultimo:%d/%m} no se parecen a los del entrenamiento. '
                  f'Revisa los datos de origen o reentrena ({MODELO_BASE}).', 'aviso', problemas, tipo='drift')


if __name__ == '__main__':
    main()
