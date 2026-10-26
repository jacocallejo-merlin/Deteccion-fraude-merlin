"""
Simula el trabajo de los analistas de fraude (Hito 6, human-in-the-loop).

No hay analistas reales, así que se reproduce lo que harían: al cerrar cada día revisan las
transacciones con la puntuación más alta (feedback.presupuesto, el 1 % ~ 7 al día), y entre unas
horas y dos días después dan su veredicto. El veredicto es la etiqueta real con algún error
(más en los junior). Todo queda en la tabla feedback_analistas.

Las alertas se ordenan con el modelo que existía ese día: la media de los detectores hasta la
primera recalibración y después el recalibrado (recalibrar_modelo.py). Si hay varios días pendientes
(la primera vez, todo el periodo de test) se recorren en orden y se recalibra cada semana por el
camino, igual que si el pipeline hubiera corrido a diario.

Presupuesto de exploración (feedback.exploracion): una vez hay recalibrador, una parte de las revisiones
de cada día (~1 de 7) no va a lo que él pone arriba, sino a lo que los detectores solos ven más raro de
lo que quedó fuera. El recalibrador solo aprende de lo que se revisa: sin esto, un tipo de fraude que deja
de puntuar alto deja de revisarse y no vuelve a aprenderlo (le pasaba al card testing: del 86 % al 27 %).

Solo revisa los días puntuados que aún no tienen feedback: se puede repetir sin duplicar nada.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG
from integrar_feedback import integrar, recuperar_revisiones_web
from recalibrar_modelo import (DETECTORES, RUTA_RECALIBRADOR, conectar, fecha_decision, inicio_test,
                               puntuar_alertas, recalibrar, toca_recalibrar, uno)

_cfg = CONFIG['feedback']
PRESUPUESTO = _cfg['presupuesto']
RETRASO_HORAS = _cfg['retraso_revision_horas']
UMBRAL_MINIMO = _cfg['umbral_minimo']
EXPLORACION = _cfg.get('exploracion', 0)
P_ERROR = _cfg['p_error']
SEMILLA = CONFIG['semilla']

COMENTARIOS = {
    (1, 'fraude'):   'Confirmado con el cliente: no reconoce la operación',
    (0, 'legitima'): 'El cliente reconoce la operación',
    (1, 'legitima'): 'Parece una compra habitual del cliente',                     # error del analista
    (0, 'fraude'):   'Operación sospechosa: se bloquea la tarjeta por precaución',  # error del analista
}


def crear_tabla(client):
    client.command('''
        CREATE TABLE IF NOT EXISTS feedback_analistas (
            transaccion_id  UInt32,
            fecha_alerta    Date,                       -- día de la transacción que saltó
            fecha_revision  DateTime,                   -- cuándo dio el veredicto el analista
            analista_id     UInt32,                     -- tabla analista
            veredicto       LowCardinality(String),     -- fraude o legitima
            tipo_fraude     LowCardinality(Nullable(String)),
            comentario      String,
            puntuacion      Float64,                    -- score con el que se priorizó la alerta
            modelo          LowCardinality(String)      -- modelo que la puso en la cola
        ) ENGINE = MergeTree
        ORDER BY (fecha_alerta, transaccion_id)
    ''')
    # Nota de riesgo de TODAS las transacciones de cada día, con el modelo que ordenaba la cola ese
    # día (la que ve la web y de la que salen las alertas). Replacing: si se vuelve a simular un
    # día, la fila nueva sustituye a la anterior.
    client.command('''
        CREATE TABLE IF NOT EXISTS riesgo_transaccion (
            transaccion_id     UInt32,
            dia                Date,
            score              Float64,                 -- nota de riesgo (más alta = más sospechosa)
            posicion           UInt32,                  -- 1 = la de más riesgo de su día
            transacciones_dia  UInt32,
            modelo             LowCardinality(String),  -- media de detectores o recalibrado_<fecha>
            en_cola            UInt8,                   -- entró en el presupuesto de los analistas
            ts                 DateTime DEFAULT now()
        ) ENGINE = ReplacingMergeTree(ts)
        ORDER BY transaccion_id
    ''')


def guardar_riesgo(client, dia, info, score, modelo, revisadas):
    """Guarda la nota y la posición en el ranking del día de todas las transacciones del día."""
    orden = np.argsort(-score, kind='stable')          # mismo orden que la cola
    posicion = np.empty(len(score), dtype='uint32')
    posicion[orden] = np.arange(1, len(score) + 1)
    client.insert_df('riesgo_transaccion', pd.DataFrame({
        'transaccion_id': info['transaccion_id'].astype('uint32').to_numpy(),
        'dia': dia, 'score': np.asarray(score, dtype=float), 'posicion': posicion,
        'transacciones_dia': np.uint32(len(score)), 'modelo': modelo,
        'en_cola': info['transaccion_id'].isin(revisadas).astype('uint8').to_numpy(),
    }))


def dias_pendientes(client):
    """Días ya puntuados por el primer detector, desde el inicio del test, que todavía no ha revisado
    nadie. (puntuar_*.py puntúan también train y calentamiento: revisar esos días no simularía nada real.)"""
    filas = client.query('''
        SELECT DISTINCT toDate(timestamp) AS dia FROM features_transaccion
        WHERE transaccion_id IN (SELECT transaccion_id FROM anomaly_scores WHERE model = {m:String})
          AND dia >= {inicio:Date}
          AND dia > (SELECT max(fecha_alerta) FROM feedback_analistas)
        ORDER BY dia
    ''', parameters={'m': DETECTORES[0], 'inicio': inicio_test()}).result_rows
    return [d for (d,) in filas]


def elegir_cola(info, score, n, explorar, umbral=0):
    """Las n transacciones que revisan los analistas: las de más score y, si explorar > 0, una parte de las
    n (al menos 1) para las que los detectores solos (info['score_detectores']) ven más raras entre las que
    se quedaron fuera. umbral: score mínimo para entrar por la parte principal (no por la exploración)."""
    todas = info.assign(puntuacion=score).sort_values('puntuacion', ascending=False, kind='stable')
    k = min(max(1, round(n * explorar)), n - 1) if explorar > 0 and n > 1 else 0
    principal = todas.head(n - k)
    if umbral:
        principal = principal[principal['puntuacion'] >= umbral]
    if not k:
        return principal.assign(explorada=False)
    resto = todas[~todas['transaccion_id'].isin(todas.head(n - k)['transaccion_id'])]
    exploradas = resto.sort_values('score_detectores', ascending=False, kind='stable').head(k)
    return pd.concat([principal.assign(explorada=False), exploradas.assign(explorada=True)])


def revisar_dia(client, dia, analistas):
    df = client.query_df('SELECT * FROM features_transaccion WHERE toDate(timestamp) = {d:Date} '
                         'ORDER BY timestamp, transaccion_id', parameters={'d': dia})
    score, modelo, info = puntuar_alertas(df)
    n = max(1, round(len(df) * PRESUPUESTO))
    recalibrado = modelo.startswith('recalibrado_')
    # Desde la fecha de decisión del ajuste, no se revisa lo que el recalibrado puntúa por debajo
    # del umbral mínimo (ajustar_umbrales.py): casi nunca era fraude. Se explora solo con recalibrador
    # (antes, la cola ya es la de los detectores)
    umbral = UMBRAL_MINIMO if UMBRAL_MINIMO and recalibrado and dia >= fecha_decision().date() else 0
    cola = elegir_cola(info, score, n, EXPLORACION if recalibrado else 0, umbral)

    rng = np.random.default_rng([SEMILLA, dia.toordinal()])    # mismo día -> mismas revisiones
    fin_dia = datetime.combine(dia + timedelta(days=1), datetime.min.time())
    filas = []
    for t in cola.itertuples():
        analista = analistas.iloc[rng.integers(len(analistas))]
        real = int(t.es_fraude)
        acierta = rng.random() >= P_ERROR[analista['nivel']]
        veredicto = 'fraude' if (real == 1) == acierta else 'legitima'
        filas.append({
            'transaccion_id': t.transaccion_id,
            'fecha_alerta': dia,
            'fecha_revision': fin_dia + timedelta(hours=float(rng.uniform(*RETRASO_HORAS))),
            'analista_id': int(analista['analista_id']),
            'veredicto': veredicto,
            'tipo_fraude': t.tipo_fraude if veredicto == 'fraude' and real else None,
            'comentario': COMENTARIOS[(real, veredicto)],
            'puntuacion': float(t.puntuacion),
            'modelo': f'{modelo}_exploracion' if t.explorada else modelo,
        })
    revision = pd.DataFrame(filas)
    guardar_riesgo(client, dia, info, score, modelo, set(cola['transaccion_id']))
    if not filas:
        print(f'{dia}: {len(df):>4} transacciones, ninguna por encima del umbral mínimo ({UMBRAL_MINIMO})')
        return revision
    client.insert_df('feedback_analistas', revision)
    n_fraude = int((revision['veredicto'] == 'fraude').sum())
    print(f'{dia}: {len(df):>4} transacciones, {len(revision)} revisadas con {modelo:<21} -> {n_fraude} fraude')
    return revision


def main():
    client = conectar()
    crear_tabla(client)
    recuperar_revisiones_web(client)
    if not uno(client, 'SELECT count() FROM feedback_analistas') and RUTA_RECALIBRADOR.exists():
        RUTA_RECALIBRADOR.unlink()   # es de una simulación anterior (base de datos regenerada)
        print('Sin feedback previo: se borra el recalibrador anterior y se empieza con la media de',
              ', '.join(DETECTORES))

    analistas = client.query_df('SELECT analista_id, nivel FROM analista WHERE activo ORDER BY analista_id')
    if analistas.empty:
        raise SystemExit('La tabla analista está vacía: carga antes el dataset (dataset.py)')

    dias = dias_pendientes(client)
    if not dias:
        print('No hay días pendientes de revisar')
        return
    print(f'Días pendientes de revisar: {len(dias)} ({dias[0]} a {dias[-1]}), '
          f'presupuesto {PRESUPUESTO:.0%} de las transacciones de cada día\n')
    for dia in dias:
        # Antes de ordenar las alertas del día, se recalibra con lo que se sabía a las 00:00
        inicio_dia = datetime.combine(dia, datetime.min.time())
        if toca_recalibrar(client, inicio_dia):
            print()
            integrar(client, inicio_dia)
            recalibrar(client, inicio_dia)
            print()
        revisar_dia(client, dia, analistas)


if __name__ == '__main__':
    main()
