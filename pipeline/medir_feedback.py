"""
¿Mejoran las alertas con el feedback de los analistas? (Hito 6)

Compara, semana a semana, la precisión de la cola de alertas (qué parte de lo revisado era fraude
real) y el recall (qué parte del fraude de la semana llegó a la cola). Se mide con la etiqueta real,
igual que el test. Para que la comparación sea justa (cada semana tiene una mezcla de fraude
distinta), en los mismos días y con el mismo presupuesto se calcula qué habrían dado:
  - kmeans:      el detector sin recalibrar (lo que había antes del Hito 6)
  - solo banco:  recalibrado en las mismas fechas pero solo con los contracargos del banco
  - con feedback: lo que realmente vieron los analistas (banco + sus veredictos)

Escribe resultados/feedback_semanal.csv, feedback_semanal.png y conclusiones_feedback.md.
"""
import sys
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_RESULTADOS
from recalibrar_modelo import MODELO_BASE, VARIABLES_EXTRA, conectar, datos_entrenamiento, entrenar, matriz

COLUMNA_BASE = f'pct_{MODELO_BASE}'


def cargar(client):
    feedback = client.query_df('''
        SELECT f.*, t.es_fraude AS real, a.nivel AS nivel
        FROM feedback_analistas f
        INNER JOIN transaccion t ON f.transaccion_id = t.transaccion_id
        INNER JOIN analista a ON f.analista_id = a.analista_id
    ''')
    if feedback.empty:
        raise SystemExit('feedback_analistas está vacía: lanza antes simular_analistas.py')
    feedback['fecha_alerta'] = feedback['fecha_alerta'].dt.date
    # todos los días desde la primera revisión, también los que no tuvieron ninguna alerta
    dias = list(pd.date_range(min(feedback['fecha_alerta']), max(feedback['fecha_alerta'])).date)
    df = client.query_df('SELECT * FROM features_transaccion WHERE toDate(timestamp) IN {d:Array(Date)} '
                         'ORDER BY timestamp, transaccion_id', parameters={'d': dias})
    X, info = matriz(df, extra=VARIABLES_EXTRA)
    info['dia'] = info['timestamp'].dt.date
    info[COLUMNA_BASE] = X[COLUMNA_BASE].to_numpy()
    return feedback, X, info


def aciertos_top(info, score, n_por_dia):
    """Fraudes reales entre las n transacciones de más score de cada día."""
    info = info.assign(score=score)
    return {dia: int(g.nlargest(n_por_dia.get(dia, 0), 'score')['es_fraude'].sum())
            for dia, g in info.groupby('dia')}


def score_solo_banco(client, feedback, X, info):
    """Score que habría dado recalibrar en las mismas fechas sin el feedback de los analistas."""
    score = info[COLUMNA_BASE].to_numpy().copy()
    modelo_dia = feedback.groupby('fecha_alerta')['modelo'].first()
    for modelo, dias in modelo_dia.groupby(modelo_dia).groups.items():
        if modelo == MODELO_BASE:
            continue
        corte = datetime.strptime(modelo.removeprefix('recalibrado_'), '%Y%m%d')
        banco = client.query_df('''
            SELECT transaccion_id, 1 AS etiqueta, 'banco' AS fuente FROM transaccion
            WHERE es_fraude = 1 AND fecha_confirmacion <= {c:DateTime}
        ''', parameters={'c': corte})
        Xe, y, revisada = datos_entrenamiento(client, corte, banco)
        filas = info['dia'].isin(set(dias)).to_numpy()
        score[filas] = entrenar(Xe, y, np.ones(len(y))).predict_proba(X.loc[filas, Xe.columns])[:, 1]
        print(f'  solo banco, corte {corte:%Y-%m-%d}: {int(y.sum())} fraudes confirmados')
    return score


def por_semana(client, feedback, X, info):
    n_por_dia = feedback.groupby('fecha_alerta').size().to_dict()
    por_dia = pd.DataFrame({
        'alertas': feedback.groupby('fecha_alerta').size(),
        'vp': feedback.groupby('fecha_alerta')['real'].sum(),
        'vp_analistas': feedback.groupby('fecha_alerta')['veredicto'].apply(lambda v: (v == 'fraude').sum()),
        'modelo': feedback.groupby('fecha_alerta')['modelo'].first(),
        'fraude_dia': info.groupby('dia')['es_fraude'].sum(),
        'vp_kmeans': pd.Series(aciertos_top(info, info[COLUMNA_BASE].to_numpy(), n_por_dia)),
        'vp_solo_banco': pd.Series(aciertos_top(info, score_solo_banco(client, feedback, X, info),
                                                n_por_dia)),
    })
    # Días sin ninguna alerta (todo por debajo del umbral mínimo): 0 alertas, el modelo del día anterior
    por_dia[['alertas', 'vp', 'vp_analistas']] = por_dia[['alertas', 'vp', 'vp_analistas']].fillna(0)
    por_dia['modelo'] = por_dia['modelo'].ffill()
    primero = por_dia.index.min()
    por_dia['semana'] = [(d - primero).days // 7 + 1 for d in por_dia.index]

    g = por_dia.groupby('semana')
    sem = pd.DataFrame({
        'desde': g.apply(lambda x: x.index.min()), 'hasta': g.apply(lambda x: x.index.max()),
        'dias': g.size(), 'modelo': g['modelo'].agg(lambda m: m.iloc[-1]),
        'alertas': g['alertas'].sum(), 'fraude_semana': g['fraude_dia'].sum(),
        'fraude_en_alertas': g['vp'].sum(),
    })
    sem['precision'] = sem['fraude_en_alertas'] / sem['alertas']
    sem['precision_segun_analistas'] = g['vp_analistas'].sum() / sem['alertas']
    sem['recall'] = sem['fraude_en_alertas'] / sem['fraude_semana']
    for nombre in ['kmeans', 'solo_banco']:
        sem[f'precision_{nombre}'] = g[f'vp_{nombre}'].sum() / sem['alertas']
        sem[f'recall_{nombre}'] = g[f'vp_{nombre}'].sum() / sem['fraude_semana']
    return sem


def grafico(sem, ruta):
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = np.arange(len(sem))
    for i, (col, nombre) in enumerate([('precision_kmeans', f'{MODELO_BASE} sin recalibrar'),
                                       ('precision_solo_banco', 'recalibrado solo con el banco'),
                                       ('precision', 'recalibrado con banco + analistas')]):
        ax.bar(x + (i - 1) * 0.27, sem[col] * 100, 0.27, label=nombre)
    ax.set_xticks(x, [f'Sem. {s}\n({d} días)' if d < 7 else f'Sem. {s}' for s, d in zip(sem.index, sem['dias'])])
    ax.set_ylabel('Alertas que eran fraude (%)')
    ax.set_title('Precisión de la cola de alertas por semana (mismo presupuesto: 1 % diario)')
    ax.legend(fontsize=8)
    ax.grid(axis='y', alpha=0.3)
    fig.tight_layout()
    fig.savefig(ruta, dpi=120)
    plt.close(fig)


def conclusiones(sem, feedback):
    completas = sem[sem['dias'] == 7]
    s1, sn = completas.iloc[0], completas.iloc[-1]
    recal = sem[sem['modelo'] != MODELO_BASE]
    vp = {'feedback': recal['fraude_en_alertas'].sum(),
          'kmeans': (recal['precision_kmeans'] * recal['alertas']).sum(),
          'banco': (recal['precision_solo_banco'] * recal['alertas']).sum()}
    alertas, fraude = recal['alertas'].sum(), recal['fraude_semana'].sum()
    errores = feedback[(feedback['veredicto'] == 'fraude') != (feedback['real'] == 1)]

    L = ['# Feedback de analistas: ¿mejoran las alertas con el tiempo?', '']
    L.append(f'- Los analistas (simulados) revisan cada día unas {len(feedback) / len(feedback["fecha_alerta"].unique()):.0f} '
             f'alertas de más puntuación: {len(feedback)} revisiones en {len(sem)} semanas.')
    L.append(f'- Semana 1 ({s1["modelo"]}, sin feedback todavía): el {s1["precision"]:.0%} de las alertas eran fraude '
             f'y llegó a la cola el {s1["recall"]:.0%} del fraude.')
    L.append(f'- Semana {completas.index[-1]} ({sn["modelo"]}): el {sn["precision"]:.0%} de las alertas eran fraude '
             f'y llegó a la cola el {sn["recall"]:.0%} del fraude.')
    L.append('- Ojo: cada semana tiene una mezcla de fraude distinta, así que comparar la semana 1 con la N '
             'directamente mezcla dos cosas. La comparación justa es en los mismos días:')
    L.append(f'- Semanas con recalibración ({len(recal)}, {alertas} alertas, {fraude} fraudes):')
    L.append(f'  - {MODELO_BASE} sin recalibrar: {vp["kmeans"]:.0f} fraudes en la cola '
             f'(precisión {vp["kmeans"] / alertas:.0%}, recall {vp["kmeans"] / fraude:.0%})')
    L.append(f'  - recalibrado solo con el banco: {vp["banco"]:.0f} '
             f'(precisión {vp["banco"] / alertas:.0%}, recall {vp["banco"] / fraude:.0%})')
    L.append(f'  - recalibrado con banco + analistas: {vp["feedback"]:.0f} '
             f'(precisión {vp["feedback"] / alertas:.0%}, recall {vp["feedback"] / fraude:.0%})')
    L.append(f'- Recalibrar con etiquetas encuentra {vp["banco"] - vp["kmeans"]:+.0f} fraudes respecto al '
             'detector: pasa de buscar lo raro a aprender qué es fraude.')
    extra = vp['feedback'] - vp['banco']
    if abs(extra) <= max(3, 0.05 * vp['banco']):
        L.append(f'- El feedback de los analistas no se nota ({extra:+.0f} fraudes frente a recalibrar solo con '
                 'el banco, dentro del ruido). Aquí el banco ya confirma la mayoría del fraude (con ~12 días '
                 'de retraso), y unas pocas revisiones al día pesan poco frente a cientos de contracargos.')
    else:
        L.append(f'- El feedback de los analistas {"añade" if extra > 0 else "quita"} {abs(extra):.0f} fraudes '
                 'frente a recalibrar solo con el banco.')
    L.append('- Donde el feedback es imprescindible es en los lotes nuevos (generar_lote.py): el banco aún no ha '
             'confirmado nada, así que los veredictos de los analistas son las únicas etiquetas recientes.')
    L.append(f'- Errores de los analistas: {len(errores)} de {len(feedback)} veredictos '
             f'({len(errores) / len(feedback):.1%}); '
             + ', '.join(f'{n} {(errores["nivel"] == n).sum()} de {(feedback["nivel"] == n).sum()}'
                         for n in sorted(feedback['nivel'].unique()))
             + '. Si el banco confirma después un fraude que el analista dio por bueno, manda el banco.')
    return '\n'.join(L) + '\n'


def main():
    client = conectar()
    feedback, X, info = cargar(client)
    print('Calculando la alternativa "solo banco" en las mismas fechas:')
    sem = por_semana(client, feedback, X, info)

    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    sem.to_csv(CARPETA_RESULTADOS / 'feedback_semanal.csv')
    grafico(sem, CARPETA_RESULTADOS / 'feedback_semanal.png')
    texto = conclusiones(sem, feedback)
    (CARPETA_RESULTADOS / 'conclusiones_feedback.md').write_text(texto, encoding='utf-8')

    tabla = sem[['desde', 'dias', 'modelo', 'alertas', 'fraude_semana', 'precision_kmeans',
                 'precision_solo_banco', 'precision', 'recall_kmeans', 'recall']]
    with pd.option_context('display.width', 200, 'display.float_format', '{:.2f}'.format):
        print(f'\n{tabla}\n')
    print(texto)
    print('Guardado en "resultados/": feedback_semanal.csv, feedback_semanal.png y conclusiones_feedback.md')


if __name__ == '__main__':
    main()
