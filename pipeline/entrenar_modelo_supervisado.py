"""Entrena el modelo supervisado elegido en la comparación: HGB + GMM.

La idea (ver README y resultados/): el GMM aporta una señal de rareza que el
HistGradientBoosting no tiene que aprender solo, y el HGB usa las etiquetas de fraude
confirmadas para ordenar las transacciones. Se validó con la misma partición temporal que
el resto de modelos (el 15 % más reciente del train) y se mide siempre en test.

Guarda, con el mismo formato que los demás modelos:
  modelos/hgb_gmm.joblib              GMM, escalador de la variable extra y HGB
  modelos/hgb_gmm_referencia.joblib   scores del ajuste ordenados (para score_pct)
  modelos/hgb_gmm_resumen.json        parámetros, PR-AUC de validación y estabilidad
  modelos/hgb_gmm_scores_{train,val,test}.parquet

No escribe en ClickHouse: los scores se insertan en anomaly_scores con
puntuar_modelo_supervisado.py. Así el entrenamiento se puede repetir aunque la base
no esté levantada; el scoring diario se encarga de refrescar la tabla.

Uso:  python pipeline/entrenar_modelo_supervisado.py
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from artefactos import crear_carpeta_run, guardar as guardar_artefacto, marcar_ultimo_run
from config import CONFIG, CARPETA_DATOS, CARPETA_MODELOS, fijar_semillas
from modelo_supervisado import construir, puntuar
from modelos_nosupervisados import normalizar, percentil, referencia

MODELO = 'hgb_gmm'
_cfg = CONFIG['modelo_supervisado']

VALIDACION = CONFIG['modelos']['proporcion_val']
SEMILLA = CONFIG['semilla']
SEMILLAS_ESTABILIDAD = _cfg.get('semillas_estabilidad', [])
VARIABLES_EXTRA = _cfg.get('variables_extra', ['n_comercios_cliente_3h'])
PARAMETROS_GMM = dict(_cfg['gmm'])
PARAMETROS_HGB = dict(_cfg['hgb'])


def _leer_extra():
    """n_comercios_cliente_3h de todas las transacciones, generado por preparacion_datos_ml.py."""
    ruta = CARPETA_DATOS / 'extra_comercios.parquet'
    if ruta.exists():
        return pd.read_parquet(ruta)
    # Compatibilidad con una base de datos ya preparada antes de existir el parquet.
    try:
        import clickhouse_connect
        from config import DATABASE, HOST, PASSWORD, PORT, USER
        client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                               password=PASSWORD, database=DATABASE)
        print('  (no existe extra_comercios.parquet: se lee n_comercios_cliente_3h de ClickHouse)')
        return client.query_df('SELECT transaccion_id, n_comercios_cliente_3h FROM features_transaccion')
    except Exception as e:
        raise SystemExit(f'Falta datos_ml/extra_comercios.parquet y no se pudo leer de ClickHouse ({e}). '
                         'Vuelve a ejecutar pipeline/preparacion_datos_ml.py.') from e


def _valores_extra(extra, ids):
    extra = extra.drop_duplicates('transaccion_id').set_index('transaccion_id')
    valores = extra['n_comercios_cliente_3h'].reindex(pd.Index(ids))
    if valores.isna().any():
        faltan = int(valores.isna().sum())
        raise ValueError(f'Faltan {faltan} valores de {VARIABLES_EXTRA[0]} para el modelo. '
                         'Vuelve a ejecutar preparacion_datos_ml.py.')
    return valores.to_numpy(dtype=float)


def cargar():
    X_train = pd.read_parquet(CARPETA_DATOS / 'X_train.parquet')
    X_test = pd.read_parquet(CARPETA_DATOS / 'X_test.parquet')
    info_train = pd.read_parquet(CARPETA_DATOS / 'info_train.parquet')
    info_test = pd.read_parquet(CARPETA_DATOS / 'info_test.parquet')
    if not X_train.index.equals(info_train.index):
        raise ValueError('X_train e info_train no tienen las mismas filas. '
                         'Vuelve a ejecutar preparacion_datos_ml.py')

    extra = _leer_extra()
    extra_train = _valores_extra(extra, info_train['transaccion_id'])
    extra_test = _valores_extra(extra, info_test['transaccion_id'])

    corte = int(len(X_train) * (1 - VALIDACION))
    X_fit, X_val = X_train.iloc[:corte], X_train.iloc[corte:]
    extra_fit, extra_val = extra_train[:corte], extra_train[corte:]
    y_fit = info_train['es_fraude'].iloc[:corte].astype(int).to_numpy()
    y_val = info_train['es_fraude'].iloc[corte:].astype(int).to_numpy()
    if y_fit.sum() == 0 or y_val.sum() == 0:
        raise ValueError('El ajuste y la validación necesitan fraudes confirmados en las dos partes')

    print(f'Ajuste:     {len(X_fit):,} filas, {int(y_fit.sum())} fraudes confirmados')
    print(f'Validación: {len(X_val):,} filas, {int(y_val.sum())} fraudes confirmados '
          f'(las más recientes del train)')
    print(f'Test:       {len(X_test):,} filas')
    return (X_train, X_test, info_test, X_fit, X_val, extra_train, extra_test,
            extra_fit, extra_val, y_fit, y_val)


def estabilidad(X_fit, extra_fit, y_fit, X_val, extra_val, y_val, ap_elegida):
    """Reentrena con semillas extra para comprobar que el resultado no depende del azar.

    La media incluye la semilla elegida (la global del proyecto): así no se compara una
    semilla afortunada contra varias desafortunadas.
    """
    if not SEMILLAS_ESTABILIDAD:
        return float(ap_elegida), 0.0
    aps = [float(ap_elegida)]
    for semilla in SEMILLAS_ESTABILIDAD:
        modelo = construir(X_fit, extra_fit, y_fit, PARAMETROS_GMM, PARAMETROS_HGB,
                           semilla, columnas=list(X_fit.columns))
        s, _ = puntuar(modelo, X_val, extra_val)
        aps.append(float(average_precision_score(y_val, s)))
    media, std = float(np.mean(aps)), float(np.std(aps))
    print(f'Estabilidad con {len(aps)} semillas: PR-AUC val {media:.4f} ± {std:.4f}')
    return media, std


def guardar(modelo, X_train, X_test, X_fit, X_val, extra_train, extra_test,
            extra_fit, extra_val, ap, media, std, run_id):
    os.makedirs(CARPETA_MODELOS, exist_ok=True)
    joblib.dump(modelo, CARPETA_MODELOS / f'{MODELO}.joblib')

    # Referencia = scores del ajuste, como en los clásicos y el autoencoder.
    s_fit, _ = puntuar(modelo, X_fit, extra_fit)
    ref = referencia(s_fit)
    ref_ordenada = np.sort(s_fit)
    joblib.dump(ref_ordenada, CARPETA_MODELOS / f'{MODELO}_referencia.joblib')

    conjuntos = {'train': (X_train, extra_train), 'val': (X_val, extra_val),
                 'test': (X_test, extra_test)}
    scores = {}
    for parte, (X, extra) in conjuntos.items():
        s, score_gmm = puntuar(modelo, X, extra)
        scores[parte] = pd.DataFrame({
            'score': s,
            'score_norm': normalizar(s, ref['mediana'], ref['p99']),
            'score_pct': percentil(s, ref_ordenada),
            'gmm_score': score_gmm,
        }, index=X.index)
        scores[parte].to_parquet(CARPETA_MODELOS / f'{MODELO}_scores_{parte}.parquet')

    parametros = {**PARAMETROS_GMM, **PARAMETROS_HGB}
    resumen = {
        'mejores_parametros': parametros,
        'parametros_gmm': PARAMETROS_GMM,
        'parametros_hgb': PARAMETROS_HGB,
        'combinaciones_probadas': int(_cfg.get('combinaciones_probadas', 1)),
        'pr_auc_val': float(ap),
        'pr_auc_val_media_semillas': float(ap if media is None else media),
        'pr_auc_val_std_semillas': float(0.0 if std is None else std),
        'referencia_normalizacion': ref,
        'proporcion_val': VALIDACION,
        'filas_ajuste': len(X_fit),
        'filas_validacion': len(X_val),
        'variables_extra': VARIABLES_EXTRA,
        'semilla': SEMILLA,
        'semillas_estabilidad': [SEMILLA] + list(SEMILLAS_ESTABILIDAD),
        'run_id': run_id,
    }
    with open(CARPETA_MODELOS / f'{MODELO}_resumen.json', 'w', encoding='utf-8') as fh:
        json.dump(resumen, fh, indent=2, ensure_ascii=False)

    for nombre in (f'{MODELO}.joblib', f'{MODELO}_referencia.joblib', f'{MODELO}_resumen.json'):
        guardar_artefacto(CARPETA_MODELOS / nombre, run_id)
    print(f'\nGuardado en "{CARPETA_MODELOS}/": {MODELO}.joblib, {MODELO}_referencia.joblib, '
          f'{MODELO}_resumen.json y los scores de train, val y test')
    return scores


def main():
    parser = argparse.ArgumentParser(description='Entrena el modelo supervisado HGB + GMM')
    parser.parse_args()
    fijar_semillas()

    run_id = f'{MODELO}_{datetime.now():%Y%m%d_%H%M%S}'
    print(f'run_id: {run_id}')
    crear_carpeta_run(run_id)

    (X_train, X_test, _info_test, X_fit, X_val, extra_train, extra_test,
     extra_fit, extra_val, y_fit, y_val) = cargar()

    modelo = construir(X_fit, extra_fit, y_fit, PARAMETROS_GMM, PARAMETROS_HGB,
                       SEMILLA, columnas=list(X_fit.columns))
    s_val, _ = puntuar(modelo, X_val, extra_val)
    ap = float(average_precision_score(y_val, s_val))
    print(f'PR-AUC en validación: {ap:.4f}')

    media, std = estabilidad(X_fit, extra_fit, y_fit, X_val, extra_val, y_val, ap)
    guardar(modelo, X_train, X_test, X_fit, X_val, extra_train, extra_test,
            extra_fit, extra_val, ap, media, std, run_id)

    marcar_ultimo_run(run_id)
    print(f'Histórico de esta ejecución en "historico/{run_id}"')


if __name__ == '__main__':
    main()
