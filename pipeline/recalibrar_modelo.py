"""
Recalibra el orden de las alertas con lo que ya se sabe que es fraude (Hito 6, human-in-the-loop).

Los detectores (feedback.detectores: KMeans y autoencoder por defecto) no usan etiquetas: marcan lo
raro, no lo que es fraude. Cada semana se entrena encima un clasificador (HistGradientBoosting) con
las etiquetas conocidas hasta ese momento (tabla etiquetas_conocidas, ver integrar_feedback.py):
contracargos confirmados por el banco y veredictos de los analistas. Entran los percentiles de todos
los detectores y las mismas variables que los modelos: así el clasificador aprende a qué detector
hacer más caso para cada tipo de pago, en vez de obligar a elegir uno solo. Las transacciones
revisadas por un analista pesan más (feedback.peso_revisadas): son justo las que los detectores ponen
arriba, así que enseñan qué alertas fueron falsas alarmas.

Para añadir otro detector (por ejemplo, el que esté probando un compañero), basta con incluirlo en
feedback.detectores si deja <nombre>.joblib y <nombre>_referencia.joblib y modelos_nosupervisados.puntuar
sabe puntuarlo. El autoencoder se puntúa con sus propios artefactos.

Solo se recalibra si toca (cada feedback.reentrenar_cada_dias, o si algún detector se ha reentrenado).
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
MODELO_BASE = _cfg.get('modelo_base', 'kmeans')
# Varios detectores a la vez: el recalibrador aprende a ponderarlos. Si no se configura la lista,
# se mantiene el comportamiento antiguo (un único modelo_base).
DETECTORES = list(dict.fromkeys(_cfg.get('detectores') or [MODELO_BASE]))
COLUMNAS_DETECTORES = [f'pct_{nombre}' for nombre in DETECTORES]
NOMBRE_SIN_RECALIBRAR = DETECTORES[0] if len(DETECTORES) == 1 else '+'.join(DETECTORES)
REENTRENAR_CADA = timedelta(days=_cfg['reentrenar_cada_dias'])
PESO_REVISADAS = _cfg['peso_revisadas']
# Parámetros del HistGradientBoosting (feedback.modelo en config.yaml); por defecto, los originales
PARAMETROS = {'learning_rate': 0.05, 'max_iter': 200, 'max_leaf_nodes': 31, 'min_samples_leaf': 20,
              'l2_regularization': 0.0, **(_cfg.get('modelo') or {})}
# Compensar las clases (dar más peso a los fraudes) desordena el top de la cola: feedback.class_weight
# (null = sin compensar; si no está, 'balanced', el comportamiento original). Ver comparar_recalibradores.py
CLASES = _cfg.get('class_weight', 'balanced')
# Variables de features_transaccion que usa el recalibrador además de las de los modelos
# (ajustar_umbrales.py decide si alguna aporta). Van sin escalar: los árboles no lo necesitan.
VARIABLES_EXTRA = _cfg.get('variables_extra') or []
VARIABLES_QUITADAS = _cfg.get('variables_quitadas') or []
SEMILLA = CONFIG['semilla']
RUTA_RECALIBRADOR = CARPETA_MODELOS / 'recalibrador.joblib'


def conectar():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                         password=PASSWORD, database=DATABASE)


def uno(client, sql, **parametros):
    return client.query(sql, parameters=parametros).result_rows[0][0]


def run_id_detector(nombre):
    """run_id del entrenamiento de un detector: si cambia, el recalibrador ya no vale.
    Los clásicos lo guardan en resumen_entrenamiento.json y el autoencoder (y cualquier modelo que
    siga esa convención) en <nombre>_resumen.json."""
    propio = CARPETA_MODELOS / f'{nombre}_resumen.json'
    if propio.exists():
        with open(propio, encoding='utf-8') as fh:
            datos = json.load(fh)
        if datos.get('run_id'):
            return datos['run_id']
    ruta = CARPETA_MODELOS / 'resumen_entrenamiento.json'
    if ruta.exists():
        with open(ruta, encoding='utf-8') as fh:
            datos = json.load(fh)
        if nombre in datos and datos[nombre].get('run_id'):
            return datos[nombre]['run_id']
    raise SystemExit(f'Falta el run_id de "{nombre}": entrena ese detector antes de recalibrar '
                     f'(se busca modelos/{nombre}_resumen.json o su entrada en resumen_entrenamiento.json)')


def run_ids_base():
    """run_id de todos los detectores: si cambia alguno, el recalibrador ya no vale."""
    return {nombre: run_id_detector(nombre) for nombre in DETECTORES}


def reloj(client):
    """'Ahora' de la simulación: el final del último día puntuado por el primer detector."""
    ultimo = uno(client, '''
        SELECT max(toDate(timestamp)) FROM features_transaccion
        WHERE transaccion_id IN (SELECT transaccion_id FROM anomaly_scores WHERE model = {m:String})
    ''', m=DETECTORES[0])
    return datetime.combine(ultimo + timedelta(days=1), datetime.min.time())


def inicio_test():
    """Primer día del test. Antes de él los modelos se entrenaron con esos mismos datos: los
    analistas (simulados) empiezan a revisar ese día."""
    return pd.read_parquet(CARPETA_DATOS / 'info_test.parquet', columns=['timestamp'])['timestamp'].min().date()


def fecha_decision():
    """Cuándo se ajustan umbrales y variables con lo validado (ajustar_umbrales.py): tras
    ajuste.semanas_validacion semanas de feedback. Los ajustes se aplican desde ese momento.
    Si ajuste.fecha_decision está fijada, es esa: el reentrenamiento semanal recalcula la partición con
    todos los días y movería el inicio del test, pero la decisión ya se tomó y no debe cambiar de fecha."""
    fija = CONFIG['ajuste'].get('fecha_decision')
    if fija:
        return datetime.combine(pd.Timestamp(fija).date(), datetime.min.time())
    return datetime.combine(inicio_test() + timedelta(weeks=CONFIG['ajuste']['semanas_validacion']),
                            datetime.min.time())


def variables_extra_en(corte):
    """Variables extra del recalibrador en esa fecha: las de feedback.variables_extra desde la fecha
    de decisión del ajuste (antes no se habían validado)."""
    return VARIABLES_EXTRA if corte >= fecha_decision() else []


def variables_quitadas_en(corte):
    """Variables que el recalibrador deja de usar desde la fecha de decisión del ajuste."""
    return VARIABLES_QUITADAS if corte >= fecha_decision() else []


def cargar_referencia(nombre):
    return joblib.load(CARPETA_MODELOS / f'{nombre}_referencia.joblib')


def puntuar_detector(nombre, X):
    """Score de un detector para la matriz X ya preparada y escalada.
    Los clásicos usan modelos_nosupervisados.puntuar; el autoencoder, su red y su error de
    reconstrucción. Se importa torch solo si hace falta, para que el resto siga funcionando sin él."""
    if nombre == 'autoencoder':
        import torch
        from entrenar_autoencoder import Autoencoder, error_reconstruccion
        guardado = torch.load(CARPETA_MODELOS / 'autoencoder.pt', weights_only=False)
        detector = Autoencoder(guardado['n_entrada'], guardado['capas'], guardado['cuello'],
                               activacion=guardado.get('activacion', 'tanh'),
                               dropout=guardado.get('dropout', 0.0),
                               batchnorm=guardado.get('batchnorm', False))
        detector.load_state_dict(guardado['estado'])
        detector.eval()
        return error_reconstruccion(detector, X[guardado['columnas']])
    detector = joblib.load(CARPETA_MODELOS / f'{nombre}.joblib')
    s, _ = puntuar(nombre, detector, X.to_numpy())
    return s


def matriz(df, extra=None, quitar=()):
    """Variables de los modelos + percentil de cada detector (0-1, 1 = más raro que todo el ajuste)
    + las variables extra (por defecto las de feedback.variables_extra) - las de quitar."""
    escalador = joblib.load(CARPETA_DATOS / 'escalador.joblib')
    with contextlib.redirect_stdout(io.StringIO()):     # sus mensajes se repetirían cada día
        X, info = preparar_features(df, escalador)
    # Primero se puntúa con todos los detectores y luego se añaden sus percentiles: cada detector tiene que ver
    # solo las 36 variables con que se entrenó, no los percentiles de los detectores anteriores
    pcts = {nombre: percentil(puntuar_detector(nombre, X), cargar_referencia(nombre)) for nombre in DETECTORES}
    for nombre, pct in pcts.items():
        X[f'pct_{nombre}'] = pct
    for variable in VARIABLES_EXTRA if extra is None else extra:
        X[variable] = df[variable].to_numpy(dtype=float)
    return X.drop(columns=list(quitar)), info


def score_sin_recalibrar(X):
    """Score de respaldo cuando todavía no hay recalibrador: media de los percentiles de los
    detectores. Con uno solo es exactamente su percentil, como antes."""
    columnas = [c for c in COLUMNAS_DETECTORES if c in X.columns]
    return X[columnas].mean(axis=1).to_numpy()


def cargar_recalibrador():
    """El recalibrador guardado, o None si no hay, es de otro detector o usa otras variables que
    las de config.yaml (entonces toca recalibrar). Los recalibradores antiguos (un solo detector y
    run_id_base escalar) siguen valiendo mientras la lista de detectores sea la misma."""
    if not RUTA_RECALIBRADOR.exists():
        return None
    try:
        recal = joblib.load(RUTA_RECALIBRADOR)
    except Exception as e:   # p. ej. guardado con otra versión de scikit-learn: no se puede usar, se rehace
        print(f'No se puede leer {RUTA_RECALIBRADOR.name} ({type(e).__name__}: {e}): se recalibra de nuevo')
        return None
    if 'detectores' in recal:
        obsoleto = recal['detectores'] != DETECTORES or recal.get('run_ids_base') != run_ids_base()
    else:
        obsoleto = DETECTORES != [MODELO_BASE] or recal['run_id_base'] != run_id_detector(MODELO_BASE)
    if (obsoleto
            or recal.get('variables_extra', []) != variables_extra_en(recal['corte'])
            or recal.get('variables_quitadas', []) != variables_quitadas_en(recal['corte'])):
        return None
    return recal


def puntuar_alertas(df):
    """Score con el que se ordenan las alertas: el recalibrador si lo hay, si no la media de los
    percentiles de los detectores. Devuelve (score, nombre del modelo usado, info)."""
    recal = cargar_recalibrador()
    X, info = matriz(df, extra=recal['variables_extra'] if recal else [],
                     quitar=recal['variables_quitadas'] if recal else [])
    # Lo que ven los detectores solos (sin recalibrar): lo usa el presupuesto de exploración de la cola
    info = info.assign(score_detectores=score_sin_recalibrar(X))
    if recal is None:
        return score_sin_recalibrar(X), NOMBRE_SIN_RECALIBRAR, info
    return recal['modelo'].predict_proba(X)[:, 1], f'recalibrado_{recal["corte"]:%Y%m%d}', info


def toca_recalibrar(client, corte):
    recal = cargar_recalibrador()
    if recal is not None:
        # cada semana, y también al llegar la fecha de decisión del ajuste (cambian las variables)
        return corte - recal['corte'] >= REENTRENAR_CADA or recal['corte'] < fecha_decision() <= corte
    # Sin recalibrador (o el detector ha cambiado): cuando haya una semana de feedback
    primera = uno(client, 'SELECT min(fecha_alerta) FROM feedback_analistas')
    return primera > date(1970, 1, 1) and \
        corte - datetime.combine(primera, datetime.min.time()) >= REENTRENAR_CADA


def datos_entrenamiento(client, corte, etiquetas):
    """Transacciones anteriores al corte (sin el calentamiento) con la etiqueta que se conocía y
    las variables que tocan en esa fecha.
    etiquetas: transaccion_id, etiqueta (0/1) y fuente, como en etiquetas_conocidas."""
    df = client.query_df('SELECT * FROM features_transaccion WHERE timestamp < {c:DateTime} '
                         'ORDER BY timestamp, transaccion_id', parameters={'c': corte})
    df = df[df['timestamp'] >= df['timestamp'].min() + pd.Timedelta(days=DIAS_CALENTAMIENTO)]
    X, info = matriz(df.reset_index(drop=True), extra=variables_extra_en(corte),
                     quitar=variables_quitadas_en(corte))
    info = info.merge(etiquetas, on='transaccion_id', how='left')
    # Lo que nadie ha marcado cuenta como legítimo, igual que al entrenar el detector
    y = info['etiqueta'].fillna(0).astype(int).to_numpy()
    revisada = info['fuente'].fillna('').str.contains('analista').to_numpy()
    return X, y, revisada


def entrenar(X, y, peso):
    return HistGradientBoostingClassifier(**PARAMETROS, class_weight=CLASES,
                                          random_state=SEMILLA).fit(X, y, sample_weight=peso)


def recalibrar(client, corte):
    etiquetas = client.query_df('SELECT transaccion_id, es_fraude AS etiqueta, fuente '
                                'FROM etiquetas_conocidas')
    X, y, revisada = datos_entrenamiento(client, corte, etiquetas)
    if y.sum() == 0:
        print(f'Recalibración {corte:%Y-%m-%d}: todavía no hay fraudes conocidos, se sigue con '
              f'{NOMBRE_SIN_RECALIBRAR}')
        return None
    modelo = entrenar(X, y, np.where(revisada, PESO_REVISADAS, 1.0))
    ids_base = run_ids_base()
    recal = {'modelo': modelo, 'corte': corte, 'detectores': DETECTORES,
             'run_ids_base': ids_base, 'run_id_base': ids_base.get(MODELO_BASE),
             'variables': list(X.columns), 'variables_extra': variables_extra_en(corte),
             'variables_quitadas': variables_quitadas_en(corte), 'filas': len(X),
             'fraudes_conocidos': int(y.sum()), 'revisadas': int(revisada.sum())}
    CARPETA_MODELOS.mkdir(exist_ok=True)
    joblib.dump(recal, RUTA_RECALIBRADOR)

    run_id = f'recalibrado_{corte:%Y%m%d}'
    crear_carpeta_run(run_id)
    guardar(RUTA_RECALIBRADOR, run_id)
    print(f'Recalibración con datos hasta {corte:%Y-%m-%d}: {len(X):,} transacciones, '
          f'{int(y.sum())} fraudes conocidos, {int(revisada.sum())} revisadas por analistas '
          f'(peso {PESO_REVISADAS}), detectores {", ".join(DETECTORES)} -> {RUTA_RECALIBRADOR.name}')
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
        desde = f'el recalibrado del {recal["corte"]:%Y-%m-%d}' if recal else NOMBRE_SIN_RECALIBRAR
        print(f'No toca recalibrar a {corte:%Y-%m-%d} (cada {REENTRENAR_CADA.days} días): se sigue con {desde}')


if __name__ == '__main__':
    main()
