"""
Ajuste de umbrales y variables según la validación (Hito 6).

Tras unas semanas de feedback (ajuste.semanas_validacion) el equipo revisa lo validado hasta esa
fecha (fraudes confirmados por el banco y veredictos de los analistas) y ajusta:

  1. Umbrales de las reglas (reglas_alerta.py). Para cada regla se prueba una rejilla de umbrales y
     se elige la de más F1: precisión sobre cualquier fraude y recall sobre el tipo al que apunta.
     Solo cuentan las transacciones de hace al menos ajuste.dias_madurez días, cuando el banco ya ha
     confirmado la mayoría. Si la precisión no llega a ajuste.precision_minima_regla, la regla se
     desactiva: casi todas sus alertas serían falsas.
  2. Umbral de la cola de alertas. Con los veredictos de los analistas se busca la puntuación del
     recalibrado por debajo de la cual las alertas casi nunca eran fraude: revisar menos sin dejar
     de revisar más de ajuste.perdida_maxima_fraude del fraude validado.
  3. Variables del recalibrador. Importancia por permutación en las semanas de feedback: se quitan
     las que no aportan solo si así mejora la validación. Y se prueban variables nuevas
     (ajuste.variables_candidatas) para los tipos de fraude que la cola no encuentra.

Un cambio se adopta solo si mejora la validación al menos ajuste.mejora_minima. Todo se decide con
lo que se sabía en la fecha de decisión; después se comprueba en el periodo siguiente, que no se ha
usado para decidir, con la etiqueta real (igual que el test).

No toca config.yaml: imprime la propuesta para copiarla allí (como buscar_autoencoder.py) y escribe
resultados/ajuste_umbrales.md, ajuste_reglas.csv y ajuste_variables.csv.
"""
import contextlib
import io
import itertools
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.inspection import permutation_importance
from sklearn.metrics import average_precision_score, roc_auc_score

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_RESULTADOS, CONFIG
from integrar_feedback import etiquetas_conocidas_a
from preparacion_datos_ml import DIAS_CALENTAMIENTO
from recalibrar_modelo import (COLUMNAS_DETECTORES, PESO_REVISADAS, conectar, entrenar, fecha_decision,
                               inicio_test, matriz)
from reglas_alerta import PLANTILLAS, UMBRALES, UMBRALES_INICIALES

_cfg = CONFIG['ajuste']
MADUREZ = timedelta(days=_cfg['dias_madurez'])
MEJORA_MINIMA = _cfg['mejora_minima']
PRECISION_MINIMA_REGLA = _cfg['precision_minima_regla']
PERDIDA_MAXIMA = _cfg['perdida_maxima_fraude']
AHORRO_MINIMO = _cfg['ahorro_minimo']
CANDIDATAS = _cfg['variables_candidatas']
CONFIANZA_MINIMA = _cfg['confianza_minima']
N_REMUESTREOS = 200
PRESUPUESTO = CONFIG['feedback']['presupuesto']
SEMILLA = CONFIG['semilla']

# Valores que se prueban de cada umbral (los iniciales se añaden siempre)
REJILLAS = {
    'pico_gasto':         {'z_minimo': [2, 2.5, 3, 3.5, 4, 5], 'ratio_minimo': [2, 3, 4, 5, 6, 8],
                           'z_madrugada_minimo': [2, 2.5, 3], 'ratio_madrugada_minimo': [3, 4, 5]},
    'card_testing':       {'pagos_10min_minimo': [1, 2, 3, 4, 5], 'importe_maximo': [2, 5, 10, 20]},
    'cash_out_multicomercio': {'comercios_minimo': [2, 3, 4], 'ratio_minimo': [1, 1.5, 2, 3]},
    'smurfing':           {'importe_minimo': [700, 800, 850, 900, 950], 'previos_24h_minimo': [1, 2, 3],
                           'ratio_minimo': [5, 10, 15, 20]},
    'account_takeover':   {'intentos_login_minimo': [2, 3, 4, 5, 6]},
    'ip_sospechosa':      {},
    'dispositivo_nuevo':  {'ratio_minimo': [1, 1.5, 2, 3, 4, 5]},
    'devolucion_abusiva': {'devoluciones_30d_minimo': [1, 2, 3, 4, 5, 6]},
    'bot':                {'seg_entre_eventos_maximo': [1, 2, 3, 5], 'eventos_minimo': [3, 4, 5, 6, 8]},
    'bust_out':           {'pct_limite_minimo': [0.6, 0.7, 0.8, 0.85, 0.9, 0.95]},
}
IMPORTES_SMURFING = sorted(set(REJILLAS['smurfing']['importe_minimo'])
                           | {UMBRALES_INICIALES['smurfing']['importe_minimo']})


# ------------------------------------------------------------------------------------- datos
def texto(serie):
    """Columna de texto con None en los vacíos (con pd.NA, comparar con == da NA y no True/False)."""
    return serie.astype(object).where(serie.notna(), None)


def cargar(client):
    """features_transaccion sin el calentamiento, con las compras previas de la tarjeta en la franja
    del smurfing para cada importe mínimo de la rejilla (la regla depende de ese umbral)."""
    previos = ', '.join(f'countIf(importe >= {v} AND importe < 1000) OVER w AS n_previos_umbral_24h_{v}'
                        for v in IMPORTES_SMURFING)
    df = client.query_df(f'''
        SELECT *, {previos} FROM features_transaccion
        WINDOW w AS (PARTITION BY metodo_id ORDER BY timestamp RANGE BETWEEN 86400 PRECEDING AND 1 PRECEDING)
        ORDER BY timestamp, transaccion_id
    ''')
    df = df[df['timestamp'] >= df['timestamp'].min() + pd.Timedelta(days=DIAS_CALENTAMIENTO)]
    df = df.reset_index(drop=True)
    df['tipo_fraude'] = texto(df['tipo_fraude'])
    return df


def etiquetas(df, conocidas):
    """Por fila de df: ¿es fraude conocido?, su tipo conocido y si la revisó un analista."""
    fraude = conocidas[conocidas['es_fraude'] == 1]
    tipo = dict(zip(fraude['transaccion_id'], fraude['tipo_fraude']))
    revisadas = set(conocidas.loc[conocidas['fuente'].str.contains('analista'), 'transaccion_id'])
    ids = df['transaccion_id']
    return (ids.isin(list(tipo)).to_numpy(), texto(ids.map(tipo)).to_numpy(),
            ids.isin(revisadas).to_numpy())


# ------------------------------------------------------------------------- 1. umbrales de reglas
def marca(df, nombre, umbrales):
    """Transacciones que dispara la regla con esos umbrales: la MISMA condición SQL de
    reglas_alerta.py, evaluada con pandas."""
    condicion = (PLANTILLAS[nombre][2].format(**umbrales)
                 .replace(' AND ', ' and ').replace(' OR ', ' or ').replace(' = ', ' == '))
    if nombre == 'smurfing':
        df = df.assign(n_previos_umbral_24h=df[f'n_previos_umbral_24h_{umbrales["importe_minimo"]}'])
    return df.eval(condicion).to_numpy(bool)


def metricas(m, fraude, del_tipo, dias):
    n, vp, vt, total_tipo = int(m.sum()), int((m & fraude).sum()), int((m & del_tipo).sum()), int(del_tipo.sum())
    p = vp / n if n else 0.0
    r = vt / total_tipo if total_tipo else 0.0
    return {'alertas_dia': n / dias, 'precision': p, 'recall_tipo': r,
            'f1': 2 * p * r / (p + r) if p + r else 0.0, 'alertas': n, 'fraudes': vp}


def combinaciones(nombre):
    inicial = UMBRALES_INICIALES[nombre]
    claves = list(REJILLAS[nombre])
    valores = [sorted(set(REJILLAS[nombre][k]) | {inicial[k]}) for k in claves]
    return [{'activa': True, **dict(zip(claves, v))} for v in itertools.product(*valores)]


def ajustar_reglas(df, fraude_conocido, tipo_conocido, decision):
    ajuste = (df['timestamp'] < decision - MADUREZ).to_numpy()
    comprobacion = (df['timestamp'] >= decision).to_numpy()
    dias_aj = df.loc[ajuste, 'timestamp'].dt.date.nunique()
    dias_co = df.loc[comprobacion, 'timestamp'].dt.date.nunique()
    fraude_real = (df['es_fraude'] == 1).to_numpy()
    tipo_real = df['tipo_fraude'].to_numpy()
    print(f'Ajuste: {dias_aj} días hasta {(decision - MADUREZ):%Y-%m-%d} con {int(fraude_conocido[ajuste].sum())} '
          f'fraudes conocidos en la fecha de decisión. Comprobación: {dias_co} días desde {decision:%Y-%m-%d}.\n')

    propuesta, filas = {}, []
    union = {'inicial': np.zeros(len(df), bool), 'ajustado': np.zeros(len(df), bool)}
    for nombre in PLANTILLAS:
        del_tipo = tipo_conocido == nombre
        evaluar = lambda u: metricas(marca(df, nombre, u)[ajuste], fraude_conocido[ajuste], del_tipo[ajuste], dias_aj)
        inicial = UMBRALES_INICIALES[nombre]
        m_ini = evaluar(inicial)
        mejor, m_mejor = inicial, m_ini
        for u in combinaciones(nombre):
            m = evaluar(u)
            if m['f1'] > m_mejor['f1'] + 1e-12:
                mejor, m_mejor = u, m
        elegido, m_eleg = (mejor, m_mejor) if m_mejor['f1'] >= m_ini['f1'] + MEJORA_MINIMA else (inicial, m_ini)
        activa = m_eleg['precision'] >= PRECISION_MINIMA_REGLA
        propuesta[nombre] = {**elegido, 'activa': activa}

        for version, u in (('inicial', inicial), ('ajustado', propuesta[nombre])):
            m_todo = marca(df, nombre, u) if u['activa'] else np.zeros(len(df), bool)
            union[version] |= m_todo
            aj = metricas(m_todo[ajuste], fraude_conocido[ajuste], del_tipo[ajuste], dias_aj)
            co = metricas(m_todo[comprobacion], fraude_real[comprobacion], tipo_real[comprobacion] == nombre, dias_co)
            filas.append({'regla': nombre, 'version': version,
                          'umbrales': ', '.join(f'{k}={v}' for k, v in u.items() if k != 'activa') or '-',
                          'activa': u['activa'],
                          **{f'validacion_{k}': v for k, v in aj.items() if k in ('alertas_dia', 'precision', 'recall_tipo', 'f1')},
                          **{f'comprobacion_{k}': v for k, v in co.items() if k in ('alertas_dia', 'precision', 'recall_tipo', 'f1')}})

    total = {}
    for version, m in union.items():
        total[version] = {'alertas_dia': m[comprobacion].sum() / dias_co,
                          'precision': (m & fraude_real)[comprobacion].sum() / max(m[comprobacion].sum(), 1),
                          'recall': (m & fraude_real)[comprobacion].sum() / fraude_real[comprobacion].sum()}
    return propuesta, pd.DataFrame(filas), total


# --------------------------------------------------------------------- 2. umbral de la cola
def ajustar_umbral_cola(client, decision):
    """Puntuación mínima del recalibrado para entrar en la cola, con los veredictos de los analistas
    conocidos en la fecha de decisión (solo alertas ordenadas por el recalibrado)."""
    fb = client.query_df('''
        SELECT puntuacion, veredicto FROM feedback_analistas
        WHERE fecha_alerta < {dia:Date} AND fecha_revision <= {corte:DateTime}
          AND startsWith(modelo, 'recalibrado_')
          AND NOT endsWith(modelo, '_exploracion')   -- las de exploración entran con puntuación baja a propósito
    ''', parameters={'dia': decision.date(), 'corte': decision})
    if fb.empty:
        return 0.0, None, 'no hay veredictos de alertas recalibradas antes de la fecha de decisión'
    fraude = (fb['veredicto'] == 'fraude').to_numpy()
    s = fb['puntuacion'].to_numpy()
    mejor_u, mejor_ahorro = 0.0, 0.0
    for u in np.unique(s):
        debajo = s < u
        if (debajo & fraude).sum() <= PERDIDA_MAXIMA * fraude.sum() and debajo.mean() > mejor_ahorro:
            mejor_u, mejor_ahorro = float(u), float(debajo.mean())
    # Tabla: alertas revisadas por tramos de puntuación y cuántas eran fraude según los analistas
    tramos = pd.qcut(s, q=min(5, len(np.unique(s))), duplicates='drop')
    tabla = (pd.DataFrame({'tramo': tramos, 'fraude': fraude}).groupby('tramo', observed=True)['fraude']
             .agg(alertas='size', fraude='sum', precision='mean'))
    if mejor_ahorro < AHORRO_MINIMO:
        motivo = (f'el mejor umbral solo ahorraría el {mejor_ahorro:.1%} de las revisiones '
                  f'(mínimo {AHORRO_MINIMO:.0%})')
        return 0.0, tabla, motivo
    u = float(np.floor(mejor_u * 1e4) / 1e4)   # redondeo hacia abajo: nunca se pierde más de lo permitido
    return u, tabla, (f'por debajo de {u:.4f} se revisaba el {mejor_ahorro:.0%} de las alertas y como mucho el '
                      f'{PERDIDA_MAXIMA:.0%} del fraude validado')


# ------------------------------------------------------------------- 3. variables del recalibrador
def prob_mejora(y, s_a, s_b, n=N_REMUESTREOS):
    """Fracción de remuestreos de la validación (bootstrap, por separado fraudes y legítimas) en que
    el score b tiene más PR-AUC que el a. Con 0,5 la diferencia es puro azar."""
    rng = np.random.default_rng(SEMILLA)
    fraudes, legitimas = np.flatnonzero(y), np.flatnonzero(~y)
    gana = 0
    for _ in range(n):
        i = np.r_[rng.choice(fraudes, len(fraudes)), rng.choice(legitimas, len(legitimas))]
        gana += average_precision_score(y[i], s_b[i]) > average_precision_score(y[i], s_a[i])
    return gana / n


def cola_por_dia(dias, score, n_por_dia, umbral=0.0):
    """Máscara de las alertas: las n de más puntuación de cada día (y por encima del umbral)."""
    orden = pd.DataFrame({'dia': dias, 's': score}).sort_values('s', ascending=False, kind='stable')
    rango = orden.groupby('dia').cumcount()
    elegidas = orden.index[rango.to_numpy() < orden['dia'].map(n_por_dia).to_numpy()]
    m = np.zeros(len(score), bool)
    m[elegidas] = True
    return m & (score >= umbral)


def ajustar_variables(client, X, info, decision):
    """Importancia por permutación y variables nuevas, validando en las semanas de feedback
    anteriores a la fecha de decisión con lo que se sabía entonces."""
    conocidas = etiquetas_conocidas_a(client, decision)
    y, tipo, revisada = etiquetas(info, conocidas)
    t = info['timestamp']
    inicio = pd.Timestamp(inicio_test())
    ent = (t < inicio).to_numpy()
    val = ((t >= inicio) & (t < decision)).to_numpy()
    peso = np.where(revisada, PESO_REVISADAS, 1.0)
    base = [c for c in X.columns if c not in CANDIDATAS]

    def pr_auc(cols):
        modelo = entrenar(X.loc[ent, cols], y[ent], peso[ent])
        score = modelo.predict_proba(X.loc[val, cols])[:, 1]
        return average_precision_score(y[val], score), modelo, score

    ap_base, modelo, s_base = pr_auc(base)
    imp = permutation_importance(modelo, X.loc[val, base], y[val], scoring='average_precision',
                                 n_repeats=5, random_state=SEMILLA)
    importancia = pd.Series(imp.importances_mean, index=base).sort_values(ascending=False)
    sin_aporte = [c for c in importancia.index[importancia <= 0] if c not in COLUMNAS_DETECTORES]
    ap_sin, _, s_sin = pr_auc([c for c in base if c not in sin_aporte]) if sin_aporte else (ap_base, None, s_base)
    # Con pocas decenas de fraudes, +0,02 de PR-AUC puede ser suerte: solo se quitan si la mejora se
    # mantiene al remuestrear la validación
    confianza = prob_mejora(y[val], s_base, s_sin) if sin_aporte else 0.0
    quitar = sin_aporte if ap_sin >= ap_base + MEJORA_MINIMA and confianza >= CONFIANZA_MINIMA else []
    usadas = [c for c in base if c not in quitar]
    ap_usadas = ap_sin if quitar else ap_base

    # Tipos de fraude conocidos en la validación que no llegaron a la cola de los analistas
    revisadas_fb = set(client.query_df('SELECT transaccion_id FROM feedback_analistas WHERE fecha_alerta < {d:Date}',
                                       parameters={'d': decision.date()})['transaccion_id'])
    en_cola = info['transaccion_id'].isin(revisadas_fb).to_numpy()
    tipos = (pd.DataFrame({'tipo': tipo[val & y], 'en_cola': en_cola[val & y]})
             .groupby('tipo')['en_cola'].agg(conocidos='size', en_cola='mean'))
    se_escapan = tipos[(tipos['conocidos'] >= 3) & (tipos['en_cola'] < 0.5)].index.tolist()
    # Tipos con fraudes conocidos antes del test pero ninguno en la validación: una variable que solo
    # sirve para ellos parece inútil aquí (aviso para el informe)
    sin_casos = sorted(set(tipo[ent & y]) - set(tipo[val & y]) - {None})

    nuevas, filas = [], []
    legitimas = val & ~y
    for c in CANDIDATAS:
        ap_c = pr_auc(usadas + [c])[0]   # una variable nueva se añade si no empeora (ver abajo)
        separa = {tp: roc_auc_score(np.r_[np.ones((val & (tipo == tp)).sum()), np.zeros(legitimas.sum())],
                                    np.r_[X.loc[val & (tipo == tp), c], X.loc[legitimas, c]])
                  for tp in se_escapan}
        mejor_tipo = max(separa, key=separa.get) if separa else None
        adoptar = bool(mejor_tipo) and separa[mejor_tipo] >= 0.75 and ap_c >= ap_usadas - MEJORA_MINIMA
        if adoptar:
            nuevas.append(c)
        filas.append({'variable': c, 'pr_auc_validacion': ap_c, 'tipo_que_separa': mejor_tipo,
                      'auc_en_ese_tipo': separa.get(mejor_tipo), 'adoptada': adoptar})
    return {'pr_auc_base': ap_base, 'pr_auc_sin_aporte': ap_sin, 'sin_aporte': sin_aporte, 'quitar': quitar,
            'confianza_quitar': confianza,
            'importancia': importancia, 'tipos': tipos, 'se_escapan': se_escapan, 'sin_casos': sin_casos,
            'candidatas': pd.DataFrame(filas), 'nuevas': nuevas, 'variables': usadas + nuevas,
            'fraudes_validacion': int(y[val].sum()), 'fraudes_validacion_reales': int(info.loc[val, 'es_fraude'].sum())}


def comprobar(client, X, info, decision, versiones):
    """Simula las semanas posteriores a la decisión como las vive el sistema: cada 7 días se
    recalibra con las etiquetas conocidas en ese momento y se revisa la cola de cada día.
    versiones: nombre -> (columnas, umbral mínimo). Se mide con la etiqueta real."""
    t = info['timestamp']
    dias = t.dt.date.to_numpy()
    n_por_dia = {d: max(1, round(n * PRESUPUESTO)) for d, n in pd.Series(dias).value_counts().items()}
    fin = t.max()
    scores = {v: np.full(len(info), np.nan) for v in versiones}
    corte = decision
    while corte <= fin:
        conocidas = etiquetas_conocidas_a(client, corte)
        y, _, revisada = etiquetas(info, conocidas)
        ent = (t < corte).to_numpy()
        semana = ((t >= corte) & (t < corte + timedelta(days=7))).to_numpy()
        peso = np.where(revisada, PESO_REVISADAS, 1.0)
        for v, (cols, _) in versiones.items():
            modelo = entrenar(X.loc[ent, cols], y[ent], peso[ent])
            scores[v][semana] = modelo.predict_proba(X.loc[semana, cols])[:, 1]
        print(f'  recalibrado de comprobación {corte:%Y-%m-%d}: {int(y[ent].sum())} fraudes conocidos')
        corte += timedelta(days=7)

    despues = (t >= decision).to_numpy()
    real = (info['es_fraude'] == 1).to_numpy()
    tipo = info['tipo_fraude'].astype(object).to_numpy()
    resumen, por_tipo = [], {}
    for v, (_, umbral) in versiones.items():
        m = cola_por_dia(dias[despues], scores[v][despues], n_por_dia, umbral)
        r, tp = real[despues], tipo[despues]
        resumen.append({'version': v, 'alertas': int(m.sum()), 'fraudes': int((m & r).sum()),
                        'precision': (m & r).sum() / max(m.sum(), 1), 'recall': (m & r).sum() / r.sum()})
        por_tipo[v] = pd.Series({k: (m & (tp == k)).sum() / (tp == k).sum() for k in sorted(set(tp[r]))})
    return pd.DataFrame(resumen).set_index('version'), pd.DataFrame(por_tipo)


# ------------------------------------------------------------------------------------ informe
def yaml_propuesta(reglas, umbral, variables_extra, variables_quitadas):
    lineas = ['reglas:']
    for nombre, u in reglas.items():
        valores = ', '.join([f'activa: {str(u["activa"]).lower()}'] + [f'{k}: {v}' for k, v in u.items() if k != 'activa'])
        lineas.append(f'  {nombre + ":":<20}{{{valores}}}')
    lineas += ['feedback:', f'  umbral_minimo: {umbral:g}', f'  variables_extra: {variables_extra}',
               f'  variables_quitadas: {variables_quitadas}']
    return '\n'.join(lineas)


def informe(decision, reglas, tabla_reglas, total_reglas, umbral, tabla_umbral, motivo_umbral,
            var, resumen, por_tipo, propuesta_yaml):
    pct = lambda x: f'{x:.0%}'
    L = ['# Ajuste de umbrales y variables según la validación (Hito 6)', '',
         f'Fecha de decisión: **{decision:%Y-%m-%d}**, tras {CONFIG["ajuste"]["semanas_validacion"]} semanas de '
         'feedback. Se decide solo con lo que se sabía ese día (fraudes confirmados por el banco y veredictos '
         'de los analistas) y se comprueba en los días siguientes con la etiqueta real, que no se ha usado para decidir.', '',
         '## 1. Umbrales de las reglas', '',
         f'Rejilla de umbrales por regla; se elige la de más F1 (precisión sobre cualquier fraude, recall sobre su tipo) '
         f'en las transacciones de hace más de {MADUREZ.days} días. Un umbral solo cambia si el F1 mejora al menos '
         f'{MEJORA_MINIMA}; una regla se desactiva si su precisión no llega al {PRECISION_MINIMA_REGLA:.0%}.', '',
         '| Regla | Umbrales | Validación: alertas/día, precisión, recall tipo | Comprobación: alertas/día, precisión, recall tipo |',
         '|---|---|---|---|']
    for _, f in tabla_reglas.iterrows():
        estado = f['umbrales'] if f['activa'] else f'desactivada ({f["umbrales"]})'
        L.append(f'| {f["regla"]} ({f["version"]}) | {estado} | {f["validacion_alertas_dia"]:.1f}, '
                 f'{pct(f["validacion_precision"])}, {pct(f["validacion_recall_tipo"])} | '
                 f'{f["comprobacion_alertas_dia"]:.1f}, {pct(f["comprobacion_precision"])}, {pct(f["comprobacion_recall_tipo"])} |')
    ini, aju = total_reglas['inicial'], total_reglas['ajustado']
    cambiadas = [n for n in reglas if reglas[n] != UMBRALES_INICIALES[n]]
    L += ['', f'- Cambian {len(cambiadas)} de {len(reglas)} reglas: {", ".join(cambiadas) or "ninguna"}.',
          f'- En la comprobación, todas las reglas juntas: de {ini["alertas_dia"]:.1f} a {aju["alertas_dia"]:.1f} alertas al día, '
          f'precisión de {pct(ini["precision"])} a {pct(aju["precision"])} y recall de {pct(ini["recall"])} a {pct(aju["recall"])}.',
          '- La precisión de validación sale algo por debajo de la real: los fraudes que el banco aún no había '
          'confirmado cuentan como legítimos, igual que al entrenar.', '',
          '## 2. Umbral de la cola de alertas', '',
          f'Los analistas revisan cada día el {PRESUPUESTO:.0%} de transacciones con más puntuación. Con sus veredictos '
          'se mira si las alertas de puntuación baja merecen la pena.', '']
    if tabla_umbral is not None:
        L += ['| Puntuación del recalibrado | Alertas revisadas | Fraude según los analistas |', '|---|---|---|']
        L += [f'| {tramo} | {int(f["alertas"])} | {int(f["fraude"])} ({pct(f["precision"])}) |'
              for tramo, f in tabla_umbral.iterrows()]
        L.append('')
    L.append(f'- {"Se propone" if umbral else "No se propone"} umbral mínimo: {motivo_umbral}.')
    L += ['', '## 3. Variables del recalibrador', '',
          f'Validación: semanas de feedback anteriores a la decisión, con {var["fraudes_validacion"]} fraudes conocidos '
          f'(de {var["fraudes_validacion_reales"]} reales: el resto aún no estaba confirmado). PR-AUC con todas las '
          f'variables: {var["pr_auc_base"]:.3f}.', '',
          'Importancia por permutación (cuánto baja la PR-AUC de validación si se desordena la variable):', '']
    imp = var['importancia']
    L.append('- Más importantes: ' + ', '.join(f'{k} {v:.3f}' for k, v in imp.head(6).items()) + '.')
    if var['sin_aporte']:
        mejora = var['pr_auc_sin_aporte'] - var['pr_auc_base']
        if var['quitar']:
            decision_txt = 'se quitan.'
        elif mejora < MEJORA_MINIMA:
            decision_txt = f'no mejora al menos {MEJORA_MINIMA}, se mantienen todas.'
        else:
            decision_txt = (f'mejora {mejora:+.3f}, pero solo en el {var["confianza_quitar"]:.0%} de {N_REMUESTREOS} '
                            f'remuestreos de la validación (se exige el {CONFIANZA_MINIMA:.0%}): con tan pocos fraudes '
                            'puede ser suerte, se mantienen todas.')
        L.append(f'- {len(var["sin_aporte"])} variables no aportan en validación (importancia <= 0). Sin ellas la '
                 f'PR-AUC de validación es {var["pr_auc_sin_aporte"]:.3f}: ' + decision_txt)
    if var['sin_casos']:
        L.append(f'- Ojo: en la validación no hay ningún caso conocido de {", ".join(var["sin_casos"])}. Las variables '
                 'que sirven sobre todo para esos tipos parecen inútiles aquí aunque no lo sean.')
    tipos = var['tipos']
    L += ['', 'Tipos de fraude conocidos en la validación y qué parte llegó a la cola de los analistas:', '']
    L += [f'- {tp}: {int(f["conocidos"])} conocidos, {pct(f["en_cola"])} en la cola' for tp, f in tipos.sort_values('en_cola').iterrows()]
    for _, f in var['candidatas'].iterrows():
        if f['tipo_que_separa'] is None:
            L.append(f'- {f["variable"]}: no hay ningún tipo con 3 o más casos conocidos que se escape de la cola; no se añade.')
        else:
            L.append(f'- {f["variable"]}: separa {f["tipo_que_separa"]} de las legítimas con AUC {f["auc_en_ese_tipo"]:.2f}; '
                     f'PR-AUC de validación {f["pr_auc_validacion"]:.3f}. '
                     + ('Se añade.' if f['adoptada'] else 'No se añade.'))
    L += ['', '## Comprobación después de la decisión', '',
          'Semanas posteriores a la decisión, recalibrando cada 7 días con lo que se conoce en cada momento, mismo '
          'presupuesto diario. Etiqueta real.', '',
          '| Versión | Alertas | Fraudes en la cola | Precisión | Recall |', '|---|---|---|---|---|']
    L += [f'| {v} | {int(f["alertas"])} | {int(f["fraudes"])} | {pct(f["precision"])} | {pct(f["recall"])} |'
          for v, f in resumen.iterrows()]
    L.append('')
    ini = resumen.loc['inicial']
    for v, f in resumen.drop(index='inicial').iterrows():
        dif = int(f['fraudes'] - ini['fraudes'])
        ruido = abs(dif) <= max(3, 0.05 * ini['fraudes'])
        L.append(f'- {v}: {dif:+d} fraudes en la cola y {int(f["alertas"] - ini["alertas"]):+d} alertas frente a la '
                 'inicial' + (' (dentro del ruido: no se puede decir que mejore ni que empeore).' if ruido else '.'))
    L += ['', 'Recall por tipo de fraude en la cola:', '', '| Tipo | ' + ' | '.join(por_tipo.columns) + ' |',
          '|---' * (len(por_tipo.columns) + 1) + '|']
    L += [f'| {tp} | ' + ' | '.join(pct(v) for v in f) + ' |' for tp, f in por_tipo.iterrows()]
    L += ['', '## Propuesta para config.yaml', '', '```yaml', propuesta_yaml, '```', '']
    return '\n'.join(L)


def main():
    client = conectar()
    decision = fecha_decision()
    hay = client.query('SELECT count(), max(fecha_alerta) FROM feedback_analistas').result_rows[0]
    if not hay[0] or pd.Timestamp(hay[1]) < decision:
        raise SystemExit(f'Aún no hay feedback hasta la fecha de decisión ({decision:%Y-%m-%d}): '
                         'lanza antes simular_analistas.py')

    print('=' * 78)
    print(f'AJUSTE DE UMBRALES Y VARIABLES   fecha de decisión {decision:%Y-%m-%d}')
    print('=' * 78)
    df = cargar(client)
    fraude_conocido, tipo_conocido, _ = etiquetas(df, etiquetas_conocidas_a(client, decision))

    print('\n1. UMBRALES DE LAS REGLAS')
    reglas, tabla_reglas, total_reglas = ajustar_reglas(df, fraude_conocido, tipo_conocido, decision)
    with pd.option_context('display.width', 220, 'display.float_format', '{:.2f}'.format):
        print(tabla_reglas.drop(columns=[c for c in tabla_reglas if c.endswith('_f1')]).to_string(index=False))

    print('\n2. UMBRAL DE LA COLA DE ALERTAS')
    umbral, tabla_umbral, motivo = ajustar_umbral_cola(client, decision)
    if tabla_umbral is not None:
        print(tabla_umbral.to_string())
    print(f'{"Umbral propuesto: " + format(umbral, ".4f") if umbral else "Sin umbral"}: {motivo}')

    print('\n3. VARIABLES DEL RECALIBRADOR')
    with contextlib.redirect_stdout(io.StringIO()):
        X, info = matriz(df, extra=CANDIDATAS)
    var = ajustar_variables(client, X, info, decision)
    print(f'PR-AUC de validación: {var["pr_auc_base"]:.3f} con todas; {var["pr_auc_sin_aporte"]:.3f} sin las '
          f'{len(var["sin_aporte"])} que no aportan (mejor en el {var["confianza_quitar"]:.0%} de los remuestreos) '
          f'-> se quitan: {var["quitar"] or "ninguna"}')
    print(var['tipos'].to_string())
    print(var['candidatas'].to_string(index=False))

    print('\nComprobación (semanas posteriores a la decisión):')
    base = [c for c in X.columns if c not in CANDIDATAS]
    versiones = {'inicial': (base, 0.0)}
    if var['variables'] != base:
        versiones['con las variables ajustadas'] = (var['variables'], 0.0)
    if umbral:
        versiones['ajustado (variables y umbral)'] = (var['variables'], umbral)
    resumen, por_tipo = comprobar(client, X, info, decision, versiones)
    print(resumen.to_string())
    print(por_tipo.round(2).to_string())

    propuesta = yaml_propuesta(reglas, umbral, var['nuevas'], var['quitar'])
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    tabla_reglas.to_csv(CARPETA_RESULTADOS / 'ajuste_reglas.csv', index=False)
    pd.concat([var['importancia'].rename('importancia'),
               var['candidatas'].set_index('variable')], axis=1).to_csv(CARPETA_RESULTADOS / 'ajuste_variables.csv')
    texto = informe(decision, reglas, tabla_reglas, total_reglas, umbral, tabla_umbral, motivo,
                    var, resumen, por_tipo, propuesta)
    (CARPETA_RESULTADOS / 'ajuste_umbrales.md').write_text(texto, encoding='utf-8')

    print('\nPropuesta para config.yaml:\n')
    print(propuesta)
    actual = (UMBRALES == reglas and CONFIG['feedback']['umbral_minimo'] == umbral
              and (CONFIG['feedback']['variables_extra'] or []) == var['nuevas']
              and (CONFIG['feedback'].get('variables_quitadas') or []) == var['quitar'])
    print('\nconfig.yaml ' + ('ya tiene estos valores.' if actual else 'tiene otros valores: cópialos si se adoptan.'))
    print('Informe en "resultados/": ajuste_umbrales.md, ajuste_reglas.csv y ajuste_variables.csv')


if __name__ == '__main__':
    main()
