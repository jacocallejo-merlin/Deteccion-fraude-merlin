"""
Búsqueda de la mejor configuración del autoencoder. NO forma parte de main.py ni del DAG:
se lanza a mano cuando se quieren probar arquitecturas nuevas (tarda ~4 min por combinación).

Prueba todas las combinaciones de "busqueda_autoencoder.rejilla" en config.yaml, las elige
igual que los clásicos (mejor PR-AUC en validación, mismas filas) y apunta cada una en
resultados/busqueda_autoencoder.csv. No toca el modelo en uso: al final imprime el bloque
para copiar en la sección "autoencoder" de config.yaml si se quiere cambiar.

Uso:  python pipeline/buscar_autoencoder.py
"""
import itertools
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG, CARPETA_RESULTADOS, fijar_semillas
from entrenar_autoencoder import SEMILLA, cargar, describir, entrenar, error_reconstruccion

_cfg = CONFIG['busqueda_autoencoder']
REJILLA = _cfg['rejilla']
# Se reentrena la ganadora con estas semillas para ver si su resultado depende del azar
SEMILLAS_ESTABILIDAD = _cfg['semillas_estabilidad']


def combinaciones(rejilla):
    claves = list(rejilla)
    return [dict(zip(claves, valores)) for valores in itertools.product(*rejilla.values())]


def pr_auc_val(p, semilla, X_fit, X_val, X_val_completo, y_val):
    modelo, info = entrenar(X_fit, X_val, p, semilla, detalle=False)
    return average_precision_score(y_val, error_reconstruccion(modelo, X_val_completo)), info


def main():
    fijar_semillas()
    _, _, X_fit, X_val, X_val_completo, y_val, _ = cargar()
    n_entrada = X_fit.shape[1]
    candidatos = combinaciones(REJILLA)
    fecha = datetime.now().strftime('%Y-%m-%d %H:%M')
    print(f'\nBuscando configuración ({len(candidatos)} combinaciones, ~4 min cada una):')

    filas = []
    for p in candidatos:
        ap, info = pr_auc_val(p, SEMILLA, X_fit, X_val, X_val_completo, y_val)
        print(f'  {describir(n_entrada, p):<40} ruido {p["ruido"]:<5} lr {p["lr"]:<7} '
              f'épocas {info["epocas"]:>4}  pérdida val {info["perdida_val"]:.4f}  PR-AUC val {ap:.4f}')
        filas.append({'fecha': fecha, 'arquitectura': describir(n_entrada, p), **p,
                      'capas': str(p['capas']), **info, 'pr_auc_val': round(ap, 4)})

    tabla = pd.DataFrame(filas).sort_values('pr_auc_val', ascending=False)
    mejor = candidatos[int(np.argmax([f['pr_auc_val'] for f in filas]))]
    print(f'\n-> mejor: {describir(n_entrada, mejor)}  ruido {mejor["ruido"]}  lr {mejor["lr"]}  '
          f'(PR-AUC val {tabla["pr_auc_val"].iloc[0]:.4f})')

    if SEMILLAS_ESTABILIDAD:
        aps = [pr_auc_val(mejor, s, X_fit, X_val, X_val_completo, y_val)[0]
               for s in SEMILLAS_ESTABILIDAD]
        print(f'Estabilidad con {len(aps)} semillas: PR-AUC val {np.mean(aps):.4f} ± {np.std(aps):.4f}')

    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    ruta = os.path.join(CARPETA_RESULTADOS, 'busqueda_autoencoder.csv')
    tabla.to_csv(ruta, mode='a', header=not os.path.exists(ruta), index=False)
    print(f'\nResultados añadidos a {ruta}')

    print('\nPara usarla, copia esto en la sección "autoencoder" de config.yaml:')
    print(f'  capas: {list(mejor["capas"])}\n  cuello: {mejor["cuello"]}\n'
          f'  ruido: {mejor["ruido"]}\n  lr: {mejor["lr"]}')


if __name__ == '__main__':
    main()
