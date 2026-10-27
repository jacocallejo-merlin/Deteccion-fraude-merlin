"""¿Encuentra más fraude una combinación de detectores que el mejor detector solo?

Cada detector ve mejor unos tipos de fraude que otros (el GMM, el smurfing y los bots; KMeans y el autoencoder, el
dispositivo nuevo). Al puntuar un pago no se sabe de qué tipo es, así que no se puede elegir "el modelo de cada
tipo": lo que sí se puede es combinar sus notas para que cada tipo lo encuentre el que mejor lo ve.

Cada nota se pasa a percentil respecto a la validación (así las escalas son comparables) y se prueban, con todos
los grupos de 2 o más detectores no supervisados:
  max    el percentil más alto: basta con que UN detector vea el pago muy raro
  media  la media de los percentiles: tienen que verlo raro varios a la vez

Como en el resto del proyecto, se elige con la PR-AUC de VALIDACIÓN (las etiquetas que se conocían) y el test solo
mide. Un bootstrap pareado en validación dice si la mejor combinación gana de verdad al mejor detector solo o si la
diferencia puede ser suerte: con unas decenas de fraudes conocidos, elegir entre muchas combinaciones es fácil que
premie el azar.

No lo lanza main.py: es un experimento. Necesita los scores de val y test en modelos/ (python main.py).
Guarda resultados/combinaciones_detectores.csv y resultados/combinaciones_por_tipo.csv.

Uso:  python pipeline/comparar_combinaciones.py
"""
import os
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_MODELOS, CARPETA_RESULTADOS
from evaluar_modelos_nosupervisados import (N_BOOTSTRAP, PRESUPUESTO_PCT, SUPERVISADOS, ap_bootstrap, cargar,
                                            evaluar_detector, recall_por_tipo)



def a_percentil(s, referencia):
    """Fracción de la validación con nota menor o igual: 0.99 = más raro que el 99 % de la validación."""
    ordenada = np.sort(referencia)
    return np.searchsorted(ordenada, s, side='right') / len(ordenada)


def combinar(detectores, grupo, como):
    pcts = {parte: np.column_stack([a_percentil(detectores[d][parte], detectores[d]['val']) for d in grupo])
            for parte in ('val', 'test')}
    funcion = np.max if como == 'max' else np.mean
    return {parte: funcion(p, axis=1) for parte, p in pcts.items()}


def main():
    detectores, datos = cargar(CARPETA_MODELOS)
    simples = [d for d in detectores if d not in SUPERVISADOS]
    if len(simples) < 2:
        raise SystemExit('Hacen falta al menos dos detectores no supervisados entrenados (python main.py)')

    candidatos = {d: detectores[d] for d in simples}
    for k in range(2, len(simples) + 1):
        for grupo in combinations(simples, k):
            for como in ('max', 'media'):
                candidatos[f'{como}({"+".join(grupo)})'] = combinar(detectores, grupo, como)

    tabla = pd.DataFrame([evaluar_detector(n, c, datos) for n, c in candidatos.items()])
    tabla['combinacion'] = tabla['detector'].str.contains(r'\(')
    tabla = tabla.sort_values('PR-AUC_val', ascending=False).reset_index(drop=True)

    solo = tabla[~tabla['combinacion']].iloc[0]['detector']          # el detector que se elige hoy
    mejor = tabla[tabla['combinacion']].iloc[0]['detector']          # la combinación que se elegiría

    cols = ['detector', 'PR-AUC_val', 'PR-AUC', 'pres_precision_%', 'pres_recall_%']
    print(f'\n{len(candidatos) - len(simples)} combinaciones de {len(simples)} detectores '
          f'(revisando el {PRESUPUESTO_PCT:g} % de los pagos)\n')
    print('Detectores solos:')
    print(tabla.loc[~tabla['combinacion'], cols].round(3).to_string(index=False))
    print('\nMejores combinaciones en validación:')
    print(tabla.loc[tabla['combinacion'], cols].head(10).round(3).to_string(index=False))

    # ¿Gana de verdad? Bootstrap pareado en validación (por cliente), como al elegir el detector
    aps = ap_bootstrap(datos['y_val'], [candidatos[mejor]['val'], candidatos[solo]['val']], N_BOOTSTRAP,
                       datos['grupo_val'])
    dif = aps[:, 0] - aps[:, 1]
    gana = (dif > 0).mean()
    print(f'\nMejor combinación: {mejor}   frente al mejor detector solo: {solo}')
    print(f'  PR-AUC val: {tabla.set_index("detector").loc[mejor, "PR-AUC_val"]:.3f} frente a '
          f'{tabla.set_index("detector").loc[solo, "PR-AUC_val"]:.3f}; gana en el {gana * 100:.0f} % de '
          f'{N_BOOTSTRAP} remuestreos de la validación (se exige el 95 %)')
    f = tabla.set_index('detector')
    print(f'  En test (solo para medir): PR-AUC {f.loc[mejor, "PR-AUC"]:.3f} frente a {f.loc[solo, "PR-AUC"]:.3f}; '
          f'fraude encontrado {f.loc[mejor, "pres_recall_%"]:.0f} % frente a {f.loc[solo, "pres_recall_%"]:.0f} %')
    print('  -> ' + ('merece la pena: la mejora se mantiene en la validación.' if gana >= 0.95 else
                     'no está demostrado: la diferencia en validación puede ser suerte.'))

    marcas = {n: candidatos[n]['marca_presupuesto'] for n in [*simples, mejor]}
    por_tipo = recall_por_tipo(marcas, datos)
    print(f'\nFraude encontrado por tipo en test (%), revisando el {PRESUPUESTO_PCT:g} %:')
    print(por_tipo.to_string())

    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    tabla.drop(columns='combinacion').round(4).to_csv(
        os.path.join(CARPETA_RESULTADOS, 'combinaciones_detectores.csv'), index=False)
    por_tipo.to_csv(os.path.join(CARPETA_RESULTADOS, 'combinaciones_por_tipo.csv'))
    print(f'\nGuardado en "{CARPETA_RESULTADOS}/": combinaciones_detectores.csv y combinaciones_por_tipo.csv')


if __name__ == '__main__':
    main()
