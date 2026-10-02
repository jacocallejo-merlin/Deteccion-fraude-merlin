import os
import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

CARPETA_DATOS = 'datos_ml'
CARPETA_MODELOS = 'modelos'
CARPETA_RESULTADOS = 'resultados'
COLUMNA_GRUPO = 'cliente_id'   

MODELOS = ['iforest', 'kmeans', 'ocsvm']
PRESUPUESTO_PCT = 1.0
PORCENTAJES_TOP = [0.5, 1, 2, 5, 10]
N_BOOTSTRAP = 300
N_TOP_VALIDACION = 3           
TOLERANCIA_DERIVA = 1.5        
SEMILLA = 42

pd.set_option('display.width', 160)


def cargar(carpeta):
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    detectores, idx_val, primero = {}, None, None
    for m in MODELOS:
        rutas = {p: os.path.join(carpeta, f'{m}_scores_{p}.parquet') for p in ('val', 'test')}
        if not all(os.path.exists(r) for r in rutas.values()):
            print(f'  {m}: sin scores en {carpeta}/, se omite (python modelos_nosupervisados.py {m})')
            continue

        val = pd.read_parquet(rutas['val'])
        if idx_val is None:
            idx_val, primero = val.index, m
        elif not val.index.equals(idx_val):
            raise ValueError(f'{m}: los scores de validación no tienen las mismas filas que los de '
                             f'{primero}. Se reentrenó alguno con otros datos?')

        test = pd.read_parquet(rutas['test']).reindex(info_test.index)
        if test['score_norm'].isna().any():
            raise ValueError(f'{m}: los scores de test no cuadran con info_test')

        detectores[m] = {'val': val['score_norm'].to_numpy(), 'test': test['score_norm'].to_numpy()}
        if 'cluster' in test.columns:
            detectores[m]['cluster_test'] = test['cluster'].to_numpy()
    if not detectores:
        raise SystemExit(f'No hay ningún modelo entrenado en {carpeta}/')

    faltan = idx_val.difference(info_train.index)
    if len(faltan):
        raise ValueError(f'{len(faltan)} filas de validación no están en info_train')
    info_val = info_train.loc[idx_val]

    grupo = COLUMNA_GRUPO if (COLUMNA_GRUPO in info_train and COLUMNA_GRUPO in info_test) else None
    datos = {
        'y_val': info_val['es_fraude'].astype(int).to_numpy(),
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].to_numpy(),
        'ids_test': info_test['transaccion_id'].to_numpy(),
        'grupo_val': info_val[grupo].to_numpy() if grupo else None,
        'grupo_test': info_test[grupo].to_numpy() if grupo else None,
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }
    for parte in ('val', 'test'):
        if datos[f'y_{parte}'].sum() == 0:
            raise ValueError(f'No hay fraudes en {parte}: no se pueden calcular las métricas')

    print(f'Modelos:    {carpeta}/  ({", ".join(detectores)})')
    print(f'Validación: {len(datos["y_val"]):,} filas, {datos["y_val"].sum()} fraudes')
    print(f'Test:       {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes '
          f'({datos["y_test"].mean() * 100:.2f} %), {datos["dias_test"]:.0f} días')
    print(f'Bootstrap:  por {"cliente (" + grupo + ")" if grupo else "transacción"}')
    return detectores, datos


def preparar_grupos(grupos):
    codigos, _ = pd.factorize(grupos)
    orden = np.argsort(codigos, kind='stable')
    tam = np.bincount(codigos)
    inicio = np.concatenate([[0], np.cumsum(tam)[:-1]])
    return orden, inicio, tam


def remuestra(rng, n_filas, prep):
    if prep is None:
        return rng.integers(0, n_filas, n_filas)
    orden, inicio, tam = prep
    g = rng.integers(0, len(tam), len(tam))          
    t = tam[g]
    fin = np.cumsum(t)
    pos = np.repeat(inicio[g], t) + np.arange(fin[-1]) - np.repeat(fin - t, t)
    return orden[pos]


def ap_bootstrap(y, lista_scores, n, grupos=None, semilla=SEMILLA):
    rng = np.random.default_rng(semilla)
    prep = preparar_grupos(grupos) if grupos is not None else None
    res = np.empty((n, len(lista_scores)))
    b = 0
    while b < n:
        i = remuestra(rng, len(y), prep)
        if not y[i].any():
            continue
        res[b] = [average_precision_score(y[i], s[i]) for s in lista_scores]
        b += 1
    return res


def bootstrap_eleccion(tabla, detectores, datos, n, n_top=N_TOP_VALIDACION):
    nombres = tabla['detector'].head(n_top).tolist()
    aps = ap_bootstrap(datos['y_val'], [detectores[x]['val'] for x in nombres], n,
                       datos['grupo_val'])
    filas = []
    for j, nombre in enumerate(nombres):
        dif_inf, dif_sup = np.percentile(aps[:, 0] - aps[:, j], [2.5, 97.5])
        if j == 0:
            veredicto = 'elegido'
        elif dif_inf > 0:
            veredicto = 'peor'
        elif dif_sup < 0:
            veredicto = 'mejor'
        else:
            veredicto = 'empate'
        filas.append({'detector': nombre, 'PR-AUC_val': tabla['PR-AUC_val'].iloc[j],
                      'dif_inf': dif_inf, 'dif_sup': dif_sup, 'veredicto': veredicto})
    return pd.DataFrame(filas)


def umbral_presupuesto(s_val, pct):
    return np.quantile(s_val, 1 - pct / 100)


def umbral_f1(y_val, s_val):
    p, r, u = precision_recall_curve(y_val, s_val)
    f1 = 2 * p[:-1] * r[:-1] / np.clip(p[:-1] + r[:-1], 1e-12, None)
    return u[np.argmax(f1)]


def punto_operacion(y, marcadas, dias):
    vp = int((marcadas & (y == 1)).sum())
    n = int(marcadas.sum())
    prec = vp / n if n else 0.0
    rec = vp / max(1, y.sum())
    return {'marcadas_%': marcadas.mean() * 100, 'alertas_dia': n / dias,
            'precision_%': prec * 100, 'recall_%': rec * 100,
            'f1_%': 200 * prec * rec / (prec + rec) if prec + rec else 0.0}


def evaluar_detector(nombre, det, datos):
    y_val, y = datos['y_val'], datos['y_test']
    s_val, s = det['val'], det['test']

    det['marca_presupuesto'] = s >= umbral_presupuesto(s_val, PRESUPUESTO_PCT)
    det['marca_f1'] = s >= umbral_f1(y_val, s_val)

    pr = average_precision_score(y, s)
    fila = {'detector': nombre,
            'PR-AUC_val': average_precision_score(y_val, s_val),
            'PR-AUC': pr, 'ROC-AUC': roc_auc_score(y, s), 'lift': pr / y.mean()}
    for clave, marcas in (('pres', det['marca_presupuesto']), ('f1', det['marca_f1'])):
        for k, v in punto_operacion(y, marcas, datos['dias_test']).items():
            fila[f'{clave}_{k}'] = v
    return fila


def tabla_top_k(y, s):
    orden = np.argsort(-s)
    filas = []
    for pct in PORCENTAJES_TOP:
        n = max(1, int(round(len(s) * pct / 100)))
        vp = int(y[orden[:n]].sum())
        filas.append({'top_%': pct, 'revisadas': n, 'fraudes': vp,
                      'precision_%': round(vp / n * 100, 1),
                      'recall_%': round(vp / max(1, y.sum()) * 100, 1),
                      'techo_recall_%': round(min(n, y.sum()) / max(1, y.sum()) * 100, 1),
                      'lift': round(vp / n / y.mean(), 1)})
    return pd.DataFrame(filas)


def recall_por_tipo(marcas, datos):
    y, tipos = datos['y_test'], datos['tipo_test']
    fraude = y == 1
    total = pd.Series(tipos[fraude]).value_counts()
    tabla = pd.DataFrame({'n': total})
    for nombre, m in marcas.items():
        detectados = pd.Series(tipos[fraude & m]).value_counts()
        tabla[nombre] = (detectados.reindex(total.index).fillna(0) / total * 100).round(1)
    return tabla.sort_values('n', ascending=False)


def grafico_pr(detectores, datos):
    y = datos['y_test']
    fig, ax = plt.subplots(figsize=(8, 6))
    for nombre, det in detectores.items():
        p, r, _ = precision_recall_curve(y, det['test'])
        ax.plot(r, p, lw=1.6,
                label=f'{nombre} (PR-AUC {average_precision_score(y, det["test"]):.3f})')
    ax.axhline(y.mean(), ls='--', c='grey', lw=1, label=f'azar ({y.mean() * 100:.2f} %)')
    ax.set(xlabel='Recall', ylabel='Precisión', title='Curvas precisión-recall en test',
           xlim=(0, 1), ylim=(0, 1.02))
    ax.legend(fontsize=8)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(CARPETA_RESULTADOS, 'curvas_pr_test.png'), dpi=130)
    plt.close(fig)


INFO_MODELOS = {
    'iforest': ('Isolation Forest',
                'separa cada transacción del resto haciendo cortes al azar en las variables; '
                'las que quedan aisladas con muy pocos cortes son las más raras'),
    'kmeans': ('KMeans',
               'agrupa las transacciones en perfiles típicos (clusters) y marca como sospechosas '
               'las que quedan lejos de todos los perfiles'),
    'ocsvm': ('One-Class SVM',
              'dibuja una frontera alrededor de las transacciones normales y marca lo que queda fuera'),
}

TIPOS_FRAUDE = {
    'bust_out': ('el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito',
                 'la evolución del gasto del cliente frente a su propio historial'),
    'account_takeover': ('alguien roba el acceso a una cuenta y opera con ella',
                         'cambios de dispositivo, IP u horarios respecto a lo habitual del cliente'),
    'card_testing': ('muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona',
                     'número de transacciones pequeñas por tarjeta en pocos minutos'),
    'smurfing': ('una cantidad grande repartida en muchas pequeñas para no llamar la atención',
                 'número e importe acumulado de transacciones en las últimas horas'),
    'devolucion_abusiva': ('devoluciones o contracargos fraudulentos',
                           'historial de devoluciones del cliente'),
    'dispositivo_nuevo': ('operaciones desde un dispositivo que el cliente no había usado nunca',
                          'si el dispositivo es nuevo para ese cliente'),
    'pico_gasto': ('un gasto mucho mayor de lo habitual para ese cliente',
                   'el importe comparado con la media del propio cliente (z-score por cliente)'),
    'ip_sospechosa': ('operaciones desde una IP o ubicación sospechosa',
                      'variables de IP: país, si es nueva para el cliente, cuántos clientes la usan'),
    'bot': ('transacciones automáticas, muy rápidas y repetitivas',
            'tiempo desde la transacción anterior y número de transacciones por minuto'),
}


def lista(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ', '.join(xs[:-1]) + ' y ' + xs[-1]


def nombre_detector(det):
    return INFO_MODELOS.get(det, (det,))[0]


def conclusiones(tabla, eleccion, simples, elegido, datos, por_tipo, top, resumen):
    f = tabla.set_index('detector')
    e = f.loc[elegido]
    nom = nombre_detector(elegido)
    Nom = nom[0].upper() + nom[1:]
    base = datos['y_test'].mean()
    rec, prec, alertas = e['pres_recall_%'], e['pres_precision_%'], e['pres_alertas_dia']

    col = elegido
    rec_tipo = por_tipo[col] if len(por_tipo) else pd.Series(dtype=float)
    buenos = rec_tipo[rec_tipo >= 60].index.tolist()
    malos = rec_tipo[rec_tipo < 30].index.tolist()
    L = []

    L.append(f'- El mejor detector es {nom}.')
    L.append(f'- Si el equipo revisa el {PRESUPUESTO_PCT:g} % de las transacciones que el modelo ve más '
             f'sospechosas, encuentra el {rec:.0f} % del fraude, y {prec:.0f} de cada 100 '
             f'revisiones son fraude real.')
    if buenos:
        L.append(f'- Detecta bien {lista(buenos)}.')
    if malos:
        L.append(f'- Se le escapan casi por completo {lista(malos)}.')

    L.append('\n## Cómo se ha elegido')
    probadas = {m: r['combinaciones_probadas'] for m, r in resumen.items()}
    if probadas:
        L.append('- Se probaron 3 modelos de detección de anomalías, cada uno con muchas '
                 'configuraciones distintas (' + lista(f'{nombre_detector(m)} {n}'
                                                       for m, n in probadas.items())
                 + '). De cada modelo se quedó la mejor y luego se compararon los 3.')
    else:
        L.append('- Se probaron 3 modelos de detección de anomalías, cada uno con muchas '
                 'configuraciones distintas. De cada modelo se quedó la mejor y luego se '
                 'compararon los 3.')
    L.append('- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan '
             'las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.')
    L.append('- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo '
             'más reciente (test), que no se usó para nada antes. Así la medida es honesta.')

    L.append('\n## El mejor modelo y por qué')
    L.append(f'- {nom} {INFO_MODELOS[elegido][1]}.')
    L.append(f'- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos '
             f'sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar '
             f'sacaría {base:.3f} (la proporción de fraude). {Nom} saca {e["PR-AUC"]:.2f}, '
             f'unas {e["lift"]:.0f} veces más que el azar.')
    orden = f.loc[simples, 'PR-AUC'].sort_values(ascending=False)
    L.append('- Cada modelo por separado: '
             + ', '.join(f'{INFO_MODELOS[m][0]} {v:.2f}' for m, v in orden.items()) + '.')

    val = f.loc[simples, 'PR-AUC_val'].sort_values(ascending=False)
    L.append('- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC '
             + lista(f'{nombre_detector(m)} {v:.2f}' for m, v in val.items()) + ').')
    if len(por_tipo):
        n = por_tipo['n']
        detectados = {c: int(round((por_tipo[c] / 100 * n).sum())) for c in simples}
        L.append(f'- Revisando el {PRESUPUESTO_PCT:g} % de las transacciones del test, fraudes '
                 f'encontrados de {int(n.sum())}: '
                 + lista(f'{nombre_detector(c)} {detectados[c]}' for c in simples) + '.')
        mas = max(detectados, key=detectados.get)
        if detectados[elegido] >= detectados[mas]:
            L.append(f'- El test lo confirma: {nom} es el que más fraude encuentra.')
        else:
            L.append(f'- En test, {nombre_detector(mas)} encuentra algo más ({detectados[mas]} '
                     f'frente a {detectados[elegido]}). No cambia la elección: el modelo se elige '
                     f'con validación, y elegirlo mirando el test sería hacer trampa.')
        otros = por_tipo[[c for c in simples if c != col]]
        ventaja = rec_tipo - otros.max(axis=1)
        mejor_en = ventaja[ventaja >= 10].index.tolist()
        if mejor_en:
            L.append(f'- Donde más destaca sobre los otros es en {lista(mejor_en)}.')
    if 'ocsvm' in simples and orden.index[-1] == 'ocsvm':
        L.append('- One-Class SVM queda el último: se entrena con una muestra de 10.000 '
                 'transacciones (es muy lento con más) y es muy sensible a sus parámetros.')
    empatan = eleccion.loc[eleccion['veredicto'] == 'empate', 'detector'].tolist()
    if empatan:
        L.append(f'- Ojo: en validación empata con {lista(nombre_detector(x) for x in empatan)}. '
                 f'No se puede asegurar que sea mejor, solo que no es peor.')

    if len(por_tipo):
        L.append('\n## Qué fraudes detecta y cuáles no')
        for tipo, fila in por_tipo.sort_values('n', ascending=False).iterrows():
            desc = TIPOS_FRAUDE.get(tipo, ('', ''))[0]
            L.append(f'- {tipo} ({int(fila["n"])} casos{": " + desc if desc else ""}): '
                     f'detecta el {fila[col]:.0f} %.')
        if malos:
            L.append('- Los que se escapan no se arreglan cambiando de modelo: las variables '
                     'actuales no recogen lo que los hace distintos. Habría que añadir:')
            for tipo in malos:
                if tipo in TIPOS_FRAUDE:
                    L.append(f'  - {tipo}: {TIPOS_FRAUDE[tipo][1]}.')

    L.append('\n## ¿Cuánto podemos fiarnos?')
    L.append(f'- Con los datos disponibles, la PR-AUC real estaría entre {e["IC95_inf"]:.2f} y '
             f'{e["IC95_sup"]:.2f} (intervalo de confianza del 95 %).')
    dif = e['PR-AUC'] - e['PR-AUC_val']
    if dif < -0.05:
        L.append(f'- En test baja respecto a validación ({e["PR-AUC_val"]:.2f} a {e["PR-AUC"]:.2f}). '
                 f'Es en parte normal (al elegir el mejor de varios, su nota de validación sale algo '
                 f'inflada) y en parte las transacciones recientes son algo distintas.')
    else:
        L.append(f'- Da resultados parecidos en validación y test ({e["PR-AUC_val"]:.2f} y '
                 f'{e["PR-AUC"]:.2f}): funciona igual con datos nuevos.')
    if resumen and all(r['pr_auc_val_std_semillas'] <= 0.1 * r['pr_auc_val_media_semillas']
                       for r in resumen.values()):
        L.append('- Los resultados no dependen del azar interno de los modelos: con distintas '
                 'semillas salen prácticamente iguales.')
    n_val = int(datos['y_val'].sum())
    if n_val < 50:
        L.append(f'- Solo hay {n_val} fraudes en validación, así que las decisiones tienen bastante '
                 f'margen de error.')
    L.append('- Las etiquetas no son perfectas: algunas "falsas alarmas" podrían ser fraudes que '
             'nadie confirmó, así que la precisión real puede ser algo mayor.')

    t5 = top.set_index('top_%').loc[5]
    L.append(f'- Revisar el {PRESUPUESTO_PCT:g} % supone unas {alertas:.0f} alertas al día.' if alertas >= 10 else
             f'- Revisar el {PRESUPUESTO_PCT:g} % supone unas {alertas:.1f} alertas al día.')
    L.append(f'- Revisando el 5 % se llegaría al {t5["recall_%"]:.0f} % del fraude, pero solo '
             f'{t5["precision_%"]:.0f} de cada 100 revisiones serían fraude.')
    marc = e['pres_marcadas_%']
    deriva = not (PRESUPUESTO_PCT / TOLERANCIA_DERIVA <= marc <= PRESUPUESTO_PCT * TOLERANCIA_DERIVA)
    if deriva:
        L.append(f'- En el periodo de test el umbral marcó el {marc:.2f} % en lugar del '
                 f'{PRESUPUESTO_PCT:g} % previsto: las transacciones recientes son algo distintas y '
                 f'el umbral habría que reajustarlo cada cierto tiempo.')
    else:
        L.append(f'- El umbral fijado en validación marcó el {marc:.2f} % en test, cerca de lo '
                 f'previsto: se puede usar tal cual.')


    return L


class Secciones:
    def __init__(self):
        self.n = 0

    def __call__(self, titulo):
        self.n += 1
        print('\n' + '=' * 100)
        print(f'{self.n}. {titulo}')
        print('=' * 100)


def main():
    os.makedirs(CARPETA_RESULTADOS, exist_ok=True)
    seccion = Secciones()
    detectores, datos = cargar(CARPETA_MODELOS)
    simples = list(detectores)

    filas = [evaluar_detector(n, d, datos) for n, d in detectores.items()]
    tabla = pd.DataFrame(filas).sort_values('PR-AUC_val', ascending=False).reset_index(drop=True)
    elegido = tabla.loc[0, 'detector']

    aps = ap_bootstrap(datos['y_test'], [detectores[n]['test'] for n in simples],
                       N_BOOTSTRAP, datos['grupo_test'])
    inf, sup = np.percentile(aps, [2.5, 97.5], axis=0)
    tabla = tabla.merge(pd.DataFrame({'detector': simples, 'IC95_inf': inf, 'IC95_sup': sup}),
                        on='detector', how='left')
    tabla.insert(1, 'elegido', np.where(tabla['detector'] == elegido, '<--', ''))
    tabla.round(4).to_csv(os.path.join(CARPETA_RESULTADOS, 'comparativa_test.csv'), index=False)

    seccion('CALIDAD DEL RANKING EN TEST   (modelos ordenados por PR-AUC de validación)')
    cols = ['detector', 'elegido', 'PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC', 'lift']
    print(tabla[cols].round(3).to_string(index=False))
    print(f'  Base rate test: {datos["y_test"].mean():.4f} (PR-AUC de un score aleatorio).')
    print(f'  El modelo elegido es "{elegido}": mejor PR-AUC en validación.')

    seccion('¿EL ELEGIDO GANA DE VERDAD?   (bootstrap pareado en validación, '
            'dif = PR-AUC elegido - PR-AUC fila)')
    eleccion = bootstrap_eleccion(tabla, detectores, datos, N_BOOTSTRAP)
    print(eleccion.round(4).to_string(index=False))
    eleccion.round(4).to_csv(os.path.join(CARPETA_RESULTADOS, 'bootstrap_eleccion_val.csv'),
                             index=False)

    seccion('PUNTOS DE OPERACIÓN EN TEST   (umbrales fijados en validación)')
    for clave, titulo in (('pres', f'Presupuesto de revisión: {PRESUPUESTO_PCT:g} % de transacciones'),
                          ('f1', 'Umbral de F1 máximo en validación (ruidoso con pocos fraudes)')):
        print(f'\n{titulo}')
        sub = tabla[['detector'] + [c for c in tabla if c.startswith(clave + '_')]].copy()
        sub.columns = [c[len(clave) + 1:] if c.startswith(clave + '_') else c for c in sub.columns]
        print(sub.round(1).to_string(index=False))
    marcadas = tabla.set_index('detector')['pres_marcadas_%']
    fuera = marcadas[(marcadas > PRESUPUESTO_PCT * TOLERANCIA_DERIVA)
                     | (marcadas < PRESUPUESTO_PCT / TOLERANCIA_DERIVA)]
    if len(fuera):
        print(f'\n  AVISO de deriva: marcan en test lejos del {PRESUPUESTO_PCT:g} % previsto -> '
              + ', '.join(f'{n} ({v:.2f} %)' for n, v in fuera.items()))

    marcas = {n: detectores[n]['marca_presupuesto'] for n in simples}
    seccion(f'RECALL % POR TIPO DE FRAUDE (presupuesto {PRESUPUESTO_PCT:g} %; elegido: {elegido})')
    por_tipo = recall_por_tipo(marcas, datos)
    print(por_tipo.to_string())
    por_tipo.to_csv(os.path.join(CARPETA_RESULTADOS, 'recall_por_tipo_test.csv'))

    seccion(f'DETALLE DEL ELEGIDO: {elegido} - si se revisara solo el top k % del test')
    top = tabla_top_k(datos['y_test'], detectores[elegido]['test'])
    print(top.to_string(index=False))

    if 'kmeans' in simples and 'cluster_test' in detectores['kmeans']:
        cl = pd.DataFrame({'cluster': detectores['kmeans']['cluster_test'], 'fraude': datos['y_test']})
        print('\nKMeans - tasa de fraude por cluster (test):')
        print(cl.groupby('cluster')['fraude'].agg(n='size', fraudes='sum', tasa_pct='mean')
              .assign(tasa_pct=lambda t: (t['tasa_pct'] * 100).round(2)).to_string())

    grafico_pr(detectores, datos)

    ruta_resumen = os.path.join(CARPETA_MODELOS, 'resumen_entrenamiento.json')
    resumen = {}
    if os.path.exists(ruta_resumen):
        with open(ruta_resumen, encoding='utf-8') as fh:
            resumen = {m: r for m, r in json.load(fh).items() if m in simples}

    seccion('CONCLUSIONES')
    lineas = conclusiones(tabla, eleccion, simples, elegido, datos, por_tipo, top, resumen)
    print('\n'.join(lineas))
    with open(os.path.join(CARPETA_RESULTADOS, 'conclusiones.md'), 'w', encoding='utf-8') as fh:
        fh.write('# Conclusiones: detección de anomalías con modelos no supervisados\n\n')
        fh.write('\n'.join(lineas) + '\n')

if __name__ == '__main__':
    main()
