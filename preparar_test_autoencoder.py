import argparse
import os
import sys
from pathlib import Path

import joblib
import pandas as pd

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))                
sys.path.insert(0, str(RAIZ / 'pipeline'))  

from preparacion_datos_ml import (cargar_datos, quitar_calentamiento, seleccionar_variables,
                                  transformar)

CARPETA_ESCALADOR = 'datos_ml'    
CARPETA_SALIDA = 'datos_ml_nuevo'
MODELO = 'modelos/autoencoder.pt'  


def columnas_del_modelo():
    if not os.path.exists(MODELO):
        return None
    import torch
    return torch.load(MODELO, weights_only=False).get('columnas')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--salida', default=CARPETA_SALIDA)
    args = ap.parse_args()

    ruta_escalador = os.path.join(CARPETA_ESCALADOR, 'escalador.joblib')
    if not os.path.exists(ruta_escalador):
        raise SystemExit(f'No está {ruta_escalador}. Hace falta el escalador del entrenamiento '
                         f'original: sin él este conjunto no es comparable.')

    df = cargar_datos()
    df = quitar_calentamiento(df)
    X, info = seleccionar_variables(df)
    X = transformar(X)

    columnas = columnas_del_modelo()
    if columnas is not None:
        faltan = [c for c in columnas if c not in X.columns]
        sobran = [c for c in X.columns if c not in columnas]
        if faltan:
            raise SystemExit(f'Al dataset nuevo le faltan columnas que el modelo espera: {faltan}')
        if sobran:
            print(f'Aviso: el dataset nuevo trae columnas que el modelo no usa, se descartan: '
                  f'{sobran}')
        X = X[columnas]        
        print(f'\nColumnas alineadas con el modelo: {len(columnas)}')

    escalador = joblib.load(ruta_escalador)
    X_esc = pd.DataFrame(escalador.transform(X), columns=X.columns, index=X.index)

    print(f'\nEscalado con el escalador del train original (NO se reajusta).')
    print(f'  media {X_esc.values.mean():+.3f}, desviación {X_esc.values.std():.3f}')

    deriva = X_esc.mean()
    deriva = deriva[deriva.abs() > 0.5]
    if len(deriva):
        print(f'\n¡OJO! Variables que se desplazan respecto al train original:\n'
              f'{deriva.round(2).to_string()}')
    else:
        print('  Ninguna variable se aleja más de 0,5 desviaciones del train original.')

    os.makedirs(args.salida, exist_ok=True)
    X_esc.to_parquet(os.path.join(args.salida, 'X_nuevo.parquet'))
    info.to_parquet(os.path.join(args.salida, 'info_nuevo.parquet'))

    print(f'\n{len(X_esc):,} transacciones, {int(info["es_fraude"].sum())} fraudes '
          f'({info["es_fraude"].mean() * 100:.2f} %)')
    print(f'Guardado en "{args.salida}/": X_nuevo.parquet, info_nuevo.parquet')


if __name__ == '__main__':
    main()