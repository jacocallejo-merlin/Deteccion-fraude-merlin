import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd
import torch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import (CARPETA_DATOS, CARPETA_MODELOS, CARPETA_RESULTADOS,
                    HOST, PORT, USER, PASSWORD, DATABASE)
from preparacion_datos_ml import preparar_features
from modelos_nosupervisados import MODELOS, entrenar, puntuar
from modelos_nosupervisados import cargar as cargar_particiones
from entrenar_autoencoder import Autoencoder, error_reconstruccion
TOLERANCIA = 1e-6


class Informe:
    """Va imprimiendo las comprobaciones y cuenta los fallos."""
    def __init__(self):
        self.lineas, self.fallos = [], 0

    def titulo(self, texto):
        self._escribir(f'\n{texto}')

    def check(self, ok, texto, detalle=''):
        if not ok:
            self.fallos += 1
        estado = 'OK   ' if ok else 'FALLO'
        self._escribir(f'  [{estado}] {texto}' + (f'  ({detalle})' if detalle else ''))

    def _escribir(self, linea):
        print(linea)
        self.lineas.append(linea)


# ----------------------------------------------------------------------- artefactos
def comprobar_artefactos(inf):
    inf.titulo('1. ARTEFACTOS DEL ENTRENAMIENTO')
    rutas = [CARPETA_DATOS / f for f in ('X_test.parquet', 'info_test.parquet', 'escalador.joblib')]
    for m in MODELOS:
        rutas += [CARPETA_MODELOS / f'{m}.joblib',
                  CARPETA_MODELOS / f'{m}_referencia.joblib',
                  CARPETA_MODELOS / f'{m}_scores_test.parquet']
    todos = True
    for r in rutas:
        inf.check(r.exists(), r.name)
        todos &= r.exists()
    return todos


# ---------------------------------------------------------------------- consistencia
def comprobar_consistencia(inf, client):
    inf.titulo('2. CONSISTENCIA DE LOS DATOS EN CLICKHOUSE')
    n_trans = client.command('SELECT count() FROM transaccion')
    n_feat = client.command('SELECT count() FROM features_transaccion')
    inf.check(n_feat == n_trans, 'features_transaccion tiene una fila por transacción',
              f'{n_feat:,} de {n_trans:,}')

    if not client.command('EXISTS TABLE anomaly_scores'):
        inf.check(False, 'existe la tabla anomaly_scores')
        return

    filas = client.query('''
        SELECT model, count(), uniqExact(transaccion_id),
               countIf(isNaN(score_pct) OR score_pct < 0 OR score_pct > 1)
        FROM anomaly_scores GROUP BY model ORDER BY model
    ''').result_rows
    presentes = {m for m, *_ in filas}
    for m in MODELOS + ['autoencoder']:
        inf.check(m in presentes, f'{m}: tiene scores en anomaly_scores')

    # Recorre TODOS los modelos que haya en la tabla
    for m, n, distintas, fuera in filas:
        inf.check(n == distintas, f'{m}: sin duplicados', f'{n:,} filas, {distintas:,} transacciones')
        inf.check(fuera == 0, f'{m}: score_pct dentro de [0, 1]',
                  f'{fuera:,} fuera de rango' if fuera else '')
        inf.check(distintas == n_trans, f'{m}: todas las transacciones puntuadas',
                  f'{distintas:,} de {n_trans:,}')

    huerfanas = client.command('''
        SELECT count() FROM anomaly_scores
        WHERE transaccion_id NOT IN (SELECT transaccion_id FROM transaccion)
    ''')
    inf.check(huerfanas == 0, 'ningún score de una transacción inexistente',
              f'{huerfanas:,} huérfanos' if huerfanas else '')


# ------------------------------------------------------------------ reproducibilidad
def comprobar_preparacion(inf, client, X_test, info_test):
    inf.titulo('3. REPRODUCIBILIDAD: PREPARACIÓN DE DATOS')
    escalador = joblib.load(CARPETA_DATOS / 'escalador.joblib')
    ids = info_test['transaccion_id']

    df = client.query_df('SELECT * FROM features_transaccion')
    df = df[df['transaccion_id'].isin(ids)].reset_index(drop=True)
    inf.check(len(df) == len(ids), 'las transacciones de test siguen en features_transaccion',
              f'{len(df):,} de {len(ids):,}')
    if len(df) != len(ids):
        return

    X_nuevo, info_nuevo = preparar_features(df, escalador)
    X_nuevo.index = info_nuevo['transaccion_id'].to_numpy()
    X_ref = X_test.set_axis(ids.to_numpy(), axis=0)
    X_nuevo = X_nuevo.loc[X_ref.index, X_ref.columns]

    dif = float(np.abs(X_nuevo.to_numpy() - X_ref.to_numpy()).max())
    inf.check(dif <= TOLERANCIA, 'preparar_features reproduce X_test.parquet',
              f'diferencia máx {dif:.2e}')


def comprobar_scoring(inf, X_test):
    inf.titulo('4. REPRODUCIBILIDAD: SCORING CON LOS MODELOS CLÁSICOS GUARDADOS')
    for m in MODELOS:
        modelo = joblib.load(CARPETA_MODELOS / f'{m}.joblib')
        guardado = (pd.read_parquet(CARPETA_MODELOS / f'{m}_scores_test.parquet')
                    .reindex(X_test.index)['score'].to_numpy())
        s, _ = puntuar(m, modelo, X_test.to_numpy())
        dif = float(np.abs(s - guardado).max())
        inf.check(dif <= TOLERANCIA, f'{m}: el modelo guardado reproduce sus scores de test',
                  f'diferencia máx {dif:.2e}')


def comprobar_autoencoder(inf, X_test):
    inf.titulo('5. REPRODUCIBILIDAD: AUTOENCODER GUARDADO')
    ruta_pt = CARPETA_MODELOS / 'autoencoder.pt'
    ruta_scores = CARPETA_MODELOS / 'autoencoder_scores_test.parquet'
    if not (ruta_pt.exists() and ruta_scores.exists()):
        inf.titulo('  (no hay autoencoder entrenado: se omite)')
        return

   
    g = torch.load(ruta_pt, weights_only=False)
    modelo = Autoencoder(g['n_entrada'], g['capas'], g['cuello'])
    modelo.load_state_dict(g['estado'])

    # Mismo orden de columnas que en el entrenamiento
    error = error_reconstruccion(modelo, X_test[g['columnas']])
    guardado = pd.read_parquet(ruta_scores).reindex(X_test.index)['error'].to_numpy()
    dif = float(np.abs(error - guardado).max())
    # Tolerancia algo mayor: torch calcula en float32
    inf.check(np.allclose(error, guardado, rtol=1e-5, atol=1e-6),
              'autoencoder: el .pt guardado reproduce sus errores de test',
              f'diferencia máx {dif:.2e}')


def comprobar_reentreno(inf):
    inf.titulo('6. REPRODUCIBILIDAD: REENTRENAR LOS CLÁSICOS CON LA MISMA SEMILLA')
    with open(CARPETA_MODELOS / 'resumen_entrenamiento.json', encoding='utf-8') as fh:
        resumen = json.load(fh)
    _, X_test, X_fit, _, _, _ = cargar_particiones()

    for m in MODELOS:
        if m not in resumen:
            inf.check(False, f'{m}: tiene parámetros en resumen_entrenamiento.json')
            continue
        r = resumen[m]
        modelo = entrenar(m, r['mejores_parametros'], X_fit.to_numpy(), r['semilla'])
        s, _ = puntuar(m, modelo, X_test.to_numpy())
        guardado = (pd.read_parquet(CARPETA_MODELOS / f'{m}_scores_test.parquet')
                    .reindex(X_test.index)['score'].to_numpy())
        dif = float(np.abs(s - guardado).max())
        inf.check(dif <= TOLERANCIA, f'{m}: reentrenado con semilla {r["semilla"]} da los mismos scores',
                  f'diferencia máx {dif:.2e}')


# ---------------------------------------------------------------------------- main
def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--reentrenar', action='store_true',
                        help='reentrena los modelos clásicos para comprobar la semilla (más lento)')
    args = parser.parse_args()

    inf = Informe()
    print('=' * 78)
    print(f'VALIDACIÓN DE REPRODUCIBILIDAD Y CONSISTENCIA   {datetime.now():%Y-%m-%d %H:%M}')
    print('=' * 78)

    artefactos_ok = comprobar_artefactos(inf)
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                           password=PASSWORD, database=DATABASE)
    comprobar_consistencia(inf, client)

    if artefactos_ok:
        X_test = pd.read_parquet(CARPETA_DATOS / 'X_test.parquet')
        info_test = pd.read_parquet(CARPETA_DATOS / 'info_test.parquet')
        comprobar_preparacion(inf, client, X_test, info_test)
        comprobar_scoring(inf, X_test)
        comprobar_autoencoder(inf, X_test)
        if args.reentrenar:
            comprobar_reentreno(inf)
    else:
        inf.titulo('Faltan artefactos: se omiten las comprobaciones de reproducibilidad')

    inf.titulo('RESULTADO: ' + ('todo correcto' if inf.fallos == 0
                                else f'{inf.fallos} comprobaciones fallidas'))
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    ruta = CARPETA_RESULTADOS / f'validacion_{datetime.now():%Y%m%d_%H%M}.txt'
    ruta.write_text('\n'.join(inf.lineas) + '\n', encoding='utf-8')
    print(f'Informe guardado en {ruta}')

    sys.exit(1 if inf.fallos else 0)


if __name__ == '__main__':
    main()