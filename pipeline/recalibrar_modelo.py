"""
Recalibra el orden de las alertas con lo que ya se sabe que es fraude (Hito 6, human-in-the-loop).

El detector elegido (KMeans) no usa etiquetas: marca lo raro, no lo que es fraude. Cada semana se
entrena encima un clasificador (HistGradientBoosting) con las etiquetas conocidas hasta ese momento
(tabla etiquetas_conocidas, ver integrar_feedback.py): contracargos confirmados por el banco y
veredictos de los analistas. Entra el score del detector y las mismas variables que los modelos.
Las transacciones revisadas por un analista pesan más (feedback.peso_revisadas): son justo las que
el detector pone arriba, así que enseñan qué alertas fueron falsas alarmas.

Solo se recalibra si toca (cada feedback.reentrenar_cada_dias, o si el detector se ha reentrenado).
--forzar recalibra siempre. Guarda modelos/recalibrador.joblib y una copia en historico/.
"""
import argparse
import contextlib
import io
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_DATOS, CARPETA_MODELOS, CONFIG, DATABASE, HOST, PASSWORD, PORT, USER
from artefactos import crear_carpeta_run, guardar
from preparacion_datos_ml import DIAS_CALENTAMIENTO, preparar_features
from modelos_nosupervisados import percentil, puntuar

_cfg = CONFIG['feedback']
MODELO_BASE = _cfg['modelo_base']
REENTRENAR_CADA = timedelta(days=_cfg['reentrenar_cada_dias'])
PESO_REVISADAS = _cfg['peso_revisadas']
SEMILLA = CONFIG['semilla']
RUTA_RECALIBRADOR = CARPETA_MODELOS / 'recalibrador.joblib'


def conectar():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                         password=PASSWORD, database=DATABASE)


def uno(client, sql, **parametros):
    return client.query(sql, parameters=parametros).result_rows[0][0]


def run_id_base():
    """run_id del entrenamiento del detector: si cambia, el recalibrador ya no vale."""
    with open(CARPETA_MODELOS / 'resumen_entrenamiento.json', encoding='utf-8') as fh:
        return json.load(fh)[MODELO_BASE]['run_id']


def reloj(client):
    """'Ahora' de la simulación: el final del último día puntuado por el detector."""
    ultimo = uno(client, '''
        SELECT max(toDate(timestamp)) FROM features_transaccion
        WHERE transaccion_id IN (SELECT transaccion_id FROM anomaly_scores WHERE model = {m:String})
    ''', m=MODELO_BASE)
    return datetime.combine(ultimo + timedelta(days=1), datetime.min.time())


def matriz(df):
    """Variables de los modelos + percentil del detector (0-1, 1 = más raro que todo el ajuste)."""
    escalador = joblib.load(CARPETA_DATOS / 'escalador.joblib')
    detector = joblib.load(CARPETA_MODELOS / f'{MODELO_BASE}.joblib')
    referencia = joblib.load(CARPETA_MODELOS / f'{MODELO_BASE}_referencia.joblib')
    with contextlib.redirect_stdout(io.StringIO()):     # sus mensajes se repetirían cada día
        X, info = preparar_features(df, escalador)
    s, _ = puntuar(MODELO_BASE, detector, X.to_numpy())
    X[f'pct_{MODELO_BASE}'] = percentil(s, referencia)
    return X, info


def cargar_recalibrador():
    """El recalibrador guardado, o None si no hay o es de un detector anterior."""
    if not RUTA_RECALIBRADOR.exists():
        return None
    recal = joblib.load(RUTA_RECALIBRADOR)
    return recal if recal['run_id_base'] == run_id_base() else None


def puntuar_alertas(df):
    """Score con el que se ordenan las alertas: el recalibrador si lo hay, si no el detector.
    Devuelve (score, nombre del modelo usado, info)."""
    X, info = matriz(df)
    recal = cargar_recalibrador()
    if recal is None:
        return X[f'pct_{MODELO_BASE}'].to_numpy(), MODELO_BASE, info
    return recal['modelo'].predict_proba(X)[:, 1], f'recalibrado_{recal["corte"]:%Y%m%d}', info


def toca_recalibrar(client, corte):
    recal = cargar_recalibrador()
    if recal is not None:
        return corte - recal['corte'] >= REENTRENAR_CADA
    # Sin recalibrador (o el detector ha cambiado): cuando haya una semana de feedback
    primera = uno(client, 'SELECT min(fecha_alerta) FROM feedback_analistas')
    return primera > date(1970, 1, 1) and \
        corte - datetime.combine(primera, datetime.min.time()) >= REENTRENAR_CADA


def datos_entrenamiento(client, corte, etiquetas):
    """Transacciones anteriores al corte (sin el calentamiento) con la etiqueta que se conocía.
    etiquetas: transaccion_id, etiqueta (0/1) y fuente, como en etiquetas_conocidas."""
    df = client.query_df('SELECT * FROM features_transaccion WHERE timestamp < {c:DateTime} '
                         'ORDER BY timestamp, transaccion_id', parameters={'c': corte})
    df = df[df['timestamp'] >= df['timestamp'].min() + pd.Timedelta(days=DIAS_CALENTAMIENTO)]
    X, info = matriz(df.reset_index(drop=True))
    info = info.merge(etiquetas, on='transaccion_id', how='left')
    # Lo que nadie ha marcado cuenta como legítimo, igual que al entrenar el detector
    y = info['etiqueta'].fillna(0).astype(int).to_numpy()
    revisada = info['fuente'].fillna('').str.contains('analista').to_numpy()
    return X, y, revisada


def entrenar(X, y, peso):
    return HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, class_weight='balanced',
                                          random_state=SEMILLA).fit(X, y, sample_weight=peso)


def recalibrar(client, corte):
    etiquetas = client.query_df('SELECT transaccion_id, es_fraude AS etiqueta, fuente '
                                'FROM etiquetas_conocidas')
    X, y, revisada = datos_entrenamiento(client, corte, etiquetas)
    if y.sum() == 0:
        print(f'Recalibración {corte:%Y-%m-%d}: todavía no hay fraudes conocidos, se sigue con {MODELO_BASE}')
        return None
    modelo = entrenar(X, y, np.where(revisada, PESO_REVISADAS, 1.0))
    recal = {'modelo': modelo, 'corte': corte, 'run_id_base': run_id_base(),
             'variables': list(X.columns), 'filas': len(X), 'fraudes_conocidos': int(y.sum()),
             'revisadas': int(revisada.sum())}
    CARPETA_MODELOS.mkdir(exist_ok=True)
    joblib.dump(recal, RUTA_RECALIBRADOR)

    run_id = f'recalibrado_{corte:%Y%m%d}'
    crear_carpeta_run(run_id)
    guardar(RUTA_RECALIBRADOR, run_id)
    print(f'Recalibración con datos hasta {corte:%Y-%m-%d}: {len(X):,} transacciones, '
          f'{int(y.sum())} fraudes conocidos, {int(revisada.sum())} revisadas por analistas '
          f'(peso {PESO_REVISADAS}) -> {RUTA_RECALIBRADOR.name}')
    return recal


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--forzar', action='store_true', help='recalibra aunque no toque')
    args = parser.parse_args()

    client = conectar()
    corte = reloj(client)
    if args.forzar or toca_recalibrar(client, corte):
        recalibrar(client, corte)
    else:
        recal = cargar_recalibrador()
        desde = f'el recalibrado del {recal["corte"]:%Y-%m-%d}' if recal else MODELO_BASE
        print(f'No toca recalibrar a {corte:%Y-%m-%d} (cada {REENTRENAR_CADA.days} días): se sigue con {desde}')


if __name__ == '__main__':
    main()
