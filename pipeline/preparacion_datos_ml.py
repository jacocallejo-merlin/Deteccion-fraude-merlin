import os

import clickhouse_connect
import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG, HOST, PORT, USER, PASSWORD, DATABASE, CARPETA_DATOS
CARPETA_SALIDA = CARPETA_DATOS

PROPORCION_TRAIN = CONFIG['preparacion']['proporcion_train']
def cargar_datos():
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    df = client.query_df('SELECT * FROM features_transaccion')
    df = df.sort_values('timestamp').reset_index(drop=True)

    print(f'Filas: {len(df):,}   Columnas: {len(df.columns)}')
    print(f'Ordenado por fecha: {df["timestamp"].is_monotonic_increasing}')
    return df



IDENTIFICADORES = ['transaccion_id', 'cliente_id', 'metodo_id', 'timestamp']
ETIQUETA = ['es_fraude', 'tipo_fraude']


# SÍ entran al modelo
VARIABLES_MODELO = [
    # importe
    'log_importe', 'pct_limite_credito',
    # tiempo
    'hora', 'dia_semana', 'es_finde', 'es_madrugada', 'dias_desde_alta',
    # categóricas convertidas a 0/1
    'es_online', 'canal_app', 'canal_web', 'es_credito', 'rechazada', 'sin_autenticacion',
    # sesión
    'proxy_vpn', 'num_intentos_login', 'ip_extranjera', 'dispositivo_nuevo',
    'n_eventos_sesion', 'cambio_dato_sesion', 'min_seg_entre_eventos',
    # velocidad de la tarjeta
    'n_tarjeta_10min', 'importe_tarjeta_10min', 'n_rechazadas_tarjeta_1h',
    'n_tarjeta_24h', 'seg_desde_anterior_tarjeta',
    'n_tarjeta_1h', 'n_tarjetas_cliente_24h',
    # historial del cliente
    'n_previas_cliente', 'z_importe_cliente', 'ratio_importe_habitual',
    'n_devoluciones_30d',
]


def seleccionar_variables(df):
    X = df[VARIABLES_MODELO].astype(float)
    info = df[IDENTIFICADORES + ETIQUETA]

    print(f'\nVariables del modelo: {X.shape[1]}')
    print(f'Columnas guardadas aparte: {list(info.columns)}')
    usadas = set(VARIABLES_MODELO + IDENTIFICADORES + ETIQUETA)
    print(f'Columnas descartadas: {[c for c in df.columns if c not in usadas]}')
    return X, info



VARIABLES_LOG = ['seg_desde_anterior_tarjeta', 'min_seg_entre_eventos',
                 'importe_tarjeta_10min', 'ratio_importe_habitual']

MIN_COMPRAS_HISTORIAL = 5   
TOPE_DIAS_ALTA = 90         

def transformar(X):
    X = X.copy()

    for col in VARIABLES_LOG:
        X[col] = np.log1p(X[col])

    X['hora_sin'] = np.sin(2 * np.pi * X['hora'] / 24)
    X['hora_cos'] = np.cos(2 * np.pi * X['hora'] / 24)
    X['dia_sin'] = np.sin(2 * np.pi * (X['dia_semana'] - 1) / 7)
    X['dia_cos'] = np.cos(2 * np.pi * (X['dia_semana'] - 1) / 7)

    X['tiene_historial'] = (X['n_previas_cliente'] >= MIN_COMPRAS_HISTORIAL).astype(float)
    X['dias_desde_alta_tope'] = X['dias_desde_alta'].clip(upper=TOPE_DIAS_ALTA)

    X = X.drop(columns=['hora', 'dia_semana', 'n_previas_cliente', 'dias_desde_alta'])

    print(f'\nVariables tras transformar: {X.shape[1]}')
    print(f'Valores infinitos: {np.isinf(X).sum().sum()}   Nulos: {X.isna().sum().sum()}')
    return X


def dividir(X, info):
    corte = int(len(X) * PROPORCION_TRAIN)

    X_train, X_test = X.iloc[:corte], X.iloc[corte:]
    info_train, info_test = info.iloc[:corte], info.iloc[corte:]

    print(f'\nTrain: {len(X_train):,} filas, hasta {info_train["timestamp"].max()}')
    print(f'Test:  {len(X_test):,} filas, desde {info_test["timestamp"].min()}')
    print(f'Fraude en train: {info_train["es_fraude"].mean() * 100:.2f} % '
          f'({int(info_train["es_fraude"].sum())} transacciones)')
    print(f'Fraude en test:  {info_test["es_fraude"].mean() * 100:.2f} % '
          f'({int(info_test["es_fraude"].sum())} transacciones)')

    tipos = pd.DataFrame({'train': info_train['tipo_fraude'].value_counts(),
                          'test': info_test['tipo_fraude'].value_counts()}).fillna(0).astype(int)
    print(f'\nTransacciones de cada tipo de fraude:\n{tipos}')
    faltan = tipos.index[(tipos['train'] == 0) | (tipos['test'] == 0)].tolist()
    if faltan:
        print(f'¡OJO! Tipos sin casos en train o en test: {faltan}')
    return X_train, X_test, info_train, info_test


def escalar(X_train, X_test):
    escalador = StandardScaler()
    escalador.fit(X_train)

    X_train_esc = pd.DataFrame(escalador.transform(X_train), columns=X_train.columns, index=X_train.index)
    X_test_esc = pd.DataFrame(escalador.transform(X_test), columns=X_test.columns, index=X_test.index)

   
    print(f'\nTrain escalado: media {X_train_esc.values.mean():+.3f}, desviación {X_train_esc.values.std():.3f}')
    print(f'Test escalado:  media {X_test_esc.values.mean():+.3f}, desviación {X_test_esc.values.std():.3f}')

    deriva = X_test_esc.mean()
    deriva = deriva[deriva.abs() > 0.5]
    if len(deriva):
        print(f'¡OJO! Variables con deriva entre train y test:\n{deriva.round(2)}')
    else:
        print('Sin deriva: ninguna variable se aleja más de 0,5 desviaciones entre train y test')
    return X_train_esc, X_test_esc, escalador

def preparar_features(df, escalador):
    """Convierte filas de features_transaccion en la matriz que entra a los modelos.
    Mismas variables, mismas transformaciones y mismo escalado que en el entrenamiento.
    Se usa al puntuar transacciones nuevas (no ajusta nada, solo aplica)."""
    X, info = seleccionar_variables(df)
    X = transformar(X)
    X_esc = pd.DataFrame(escalador.transform(X), columns=X.columns, index=X.index)
    return X_esc, info

def guardar(X_train, X_test, info_train, info_test, escalador):
    os.makedirs(CARPETA_SALIDA, exist_ok=True)
    X_train.to_parquet(os.path.join(CARPETA_SALIDA, 'X_train.parquet'))
    X_test.to_parquet(os.path.join(CARPETA_SALIDA, 'X_test.parquet'))
    info_train.to_parquet(os.path.join(CARPETA_SALIDA, 'info_train.parquet'))
    info_test.to_parquet(os.path.join(CARPETA_SALIDA, 'info_test.parquet'))
    joblib.dump(escalador, os.path.join(CARPETA_SALIDA, 'escalador.joblib'))
    print(f'\nGuardado en la carpeta "{CARPETA_SALIDA}/": X_train, X_test, info_train, info_test y escalador')


DIAS_CALENTAMIENTO = CONFIG['preparacion']['dias_calentamiento']

def quitar_calentamiento(df):
    inicio = df['timestamp'].min() + pd.Timedelta(days=DIAS_CALENTAMIENTO)
    quitadas = df[df['timestamp'] < inicio]
    print(f'\nDescartados los primeros {DIAS_CALENTAMIENTO} días: {len(quitadas):,} transacciones '
          f'({int(quitadas["es_fraude"].sum())} fraudes)')
    return df[df['timestamp'] >= inicio].reset_index(drop=True)


def main():
    df = cargar_datos()
    df = quitar_calentamiento(df)
    X, info = seleccionar_variables(df)
    X = transformar(X)
    X_train, X_test, info_train, info_test = dividir(X, info)
    X_train, X_test, escalador = escalar(X_train, X_test)
    guardar(X_train, X_test, info_train, info_test, escalador)


if __name__ == '__main__':
    main()