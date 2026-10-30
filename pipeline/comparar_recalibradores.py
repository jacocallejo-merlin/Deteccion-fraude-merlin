"""¿Ordenaría mejor la cola otra configuración del recalibrador?

El HGB + GMM supervisado (entrenar_modelo_supervisado.py) se entrena UNA vez, con lo confirmado al final del
train, y aun así encuentra más fraude en el test que el recalibrador, que se reentrena cada semana con más
etiquetas. Las diferencias de configuración: el recalibrador compensa las clases (class_weight='balanced'),
usa árboles más grandes y menos regularizados, y da peso 10 a las revisiones de los analistas (que a veces
se equivocan). Este experimento prueba esas diferencias por separado.

Para cada recalibración semanal de la simulación (los modelos recalibrado_AAAAMMDD de feedback_analistas)
reentrena cada variante con las MISMAS etiquetas que se conocían ese día (integrar_feedback.etiquetas_conocidas_a)
y cuenta cuántos fraudes reales habría puesto entre las n transacciones de más nota de cada día (n = las que
revisaron los analistas ese día). Se elige con las semanas anteriores a ajuste.fecha_decision y se comprueba con
las siguientes, que no intervienen en la elección: lo mismo que ajustar_umbrales.py.

Solo lee: no toca la base de datos ni el recalibrador guardado. No lo lanza main.py.
Guarda resultados/comparar_recalibradores.csv.

Uso:  python pipeline/comparar_recalibradores.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_RESULTADOS, CONFIG
from integrar_feedback import etiquetas_conocidas_a
from medir_feedback import aciertos_top, cargar
from recalibrar_modelo import PARAMETROS, SEMILLA, conectar, datos_entrenamiento, fecha_decision

JORGE = dict(CONFIG['modelo_supervisado']['hgb'])      # los parámetros del HGB + GMM
JORGE_CLASES = JORGE.pop('class_weight', None)

# nombre: (parámetros del HistGradientBoosting, class_weight, peso de las revisadas). Valores fijos, no los de
# config.yaml, para que el experimento salga igual después de adoptar uno. 'original' es la configuración que
# había antes de este experimento (balanced y peso 10); los árboles son siempre los de feedback.modelo
VARIANTES = {
    'original (balanced, peso 10)': (PARAMETROS, 'balanced', 10),
    'sin balanced, peso 10':        (PARAMETROS, None, 10),
    'balanced, peso 3':             (PARAMETROS, 'balanced', 3),
    'sin balanced, peso 3':         (PARAMETROS, None, 3),
    'parametros HGB+GMM, peso 10':  (JORGE, JORGE_CLASES, 10),
    'parametros HGB+GMM, peso 3':   (JORGE, JORGE_CLASES, 3),
}
ORIGINAL = 'original (balanced, peso 10)'


def entrenar(X, y, revisada, parametros, clases, peso):
    return HistGradientBoostingClassifier(**parametros, class_weight=clases, random_state=SEMILLA).fit(
        X, y, sample_weight=np.where(revisada, peso, 1.0))


def main():
    client = conectar()
    feedback, X, info = cargar(client)
    n_por_dia = feedback.groupby('fecha_alerta').size().to_dict()
    modelo_dia = feedback.groupby('fecha_alerta')['modelo'].first()
    decision = fecha_decision().date()

    scores = {v: np.full(len(info), np.nan) for v in VARIANTES}
    for modelo, dias in modelo_dia.groupby(modelo_dia).groups.items():
        if not modelo.startswith('recalibrado_'):
            continue                               # la primera semana no hay recalibrador: no se compara
        corte = datetime.strptime(modelo.removeprefix('recalibrado_'), '%Y%m%d')
        etiquetas = etiquetas_conocidas_a(client, corte).rename(columns={'es_fraude': 'etiqueta'})
        Xe, y, revisada = datos_entrenamiento(client, corte, etiquetas[['transaccion_id', 'etiqueta', 'fuente']])
        filas = info['dia'].isin(set(dias)).to_numpy()
        for nombre, (parametros, clases, peso) in VARIANTES.items():
            modelo_v = entrenar(Xe, y, revisada, parametros, clases, peso)
            scores[nombre][filas] = modelo_v.predict_proba(X.loc[filas, Xe.columns])[:, 1]
        print(f'  corte {corte:%Y-%m-%d}: {int(y.sum())} fraudes conocidos, {int(revisada.sum())} revisadas')

    # Referencia: el HGB + GMM tal cual, con la nota que dejó en anomaly_scores
    hgb = client.query_df("SELECT transaccion_id, score FROM anomaly_scores WHERE model = 'hgb_gmm'")
    if len(hgb):
        scores['HGB+GMM (entrenado una vez)'] = (info[['transaccion_id']]
                                                  .merge(hgb, on='transaccion_id', how='left')['score'].to_numpy())

    comparadas = info['dia'].isin([d for d, m in modelo_dia.items() if m.startswith('recalibrado_')])
    periodos = {'eleccion': comparadas & (info['dia'] < decision), 'comprobacion': comparadas & (info['dia'] >= decision)}
    filas_tabla = []
    for nombre, s in scores.items():
        fila = {'variante': nombre}
        for periodo, mascara in periodos.items():
            sub = info[mascara.to_numpy()]
            vp = sum(aciertos_top(sub, s[mascara.to_numpy()], n_por_dia).values())
            fila[f'{periodo}_fraudes'] = vp
            fila[f'{periodo}_recall_%'] = round(100 * vp / max(1, int(sub['es_fraude'].sum())), 1)
        filas_tabla.append(fila)
    tabla = pd.DataFrame(filas_tabla)

    total = {p: int(info[m.to_numpy()]['es_fraude'].sum()) for p, m in periodos.items()}
    revisiones = {p: sum(n_por_dia.get(d, 0) for d in set(info[m.to_numpy()]['dia'])) for p, m in periodos.items()}
    print(f'\nElección (antes del {decision}): {revisiones["eleccion"]} revisiones, {total["eleccion"]} fraudes. '
          f'Comprobación (desde el {decision}): {revisiones["comprobacion"]} revisiones, '
          f'{total["comprobacion"]} fraudes.\n')
    print(tabla.to_string(index=False))

    candidatas = tabla[tabla['variante'].isin(VARIANTES)]
    mejor = candidatas.sort_values('eleccion_fraudes', ascending=False).iloc[0]
    original = tabla.set_index('variante').loc[ORIGINAL]
    print(f'\nSe elegiría "{mejor["variante"]}" (más fraudes en las semanas de elección; si empatan, la primera).')
    print(f'En la comprobación: {mejor["comprobacion_fraudes"]} fraudes frente a {original["comprobacion_fraudes"]} '
          f'de la configuración original.')

    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    tabla.to_csv(os.path.join(CARPETA_RESULTADOS, 'comparar_recalibradores.csv'), index=False)
    print(f'Guardado en "{CARPETA_RESULTADOS}/comparar_recalibradores.csv"')


if __name__ == '__main__':
    main()
