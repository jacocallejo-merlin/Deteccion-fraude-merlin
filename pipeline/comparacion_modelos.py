import os
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score

# Raíz del proyecto, donde está config.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CONFIG, CARPETA_DATOS, CARPETA_MODELOS, CARPETA_RESULTADOS
COLUMNA_GRUPO = 'cliente_id'   

MODELOS = ['iforest', 'kmeans', 'dbscan', 'ocsvm', 'autoencoder']
PRESUPUESTO_PCT = 1.0
PORCENTAJES_TOP = [0.5, 1, 2, 5, 10]
N_BOOTSTRAP = 300
SEMILLA = CONFIG['semilla']   # semilla del bootstrap (valor en config.yaml)

SCRIPT_DE = {'iforest': 'modelos_nosupervisados.py', 'kmeans': 'modelos_nosupervisados.py',
             'ocsvm': 'modelos_nosupervisados.py', 'dbscan': 'modelos_nosupervisados.py',
             'autoencoder': 'entrenar_autoencoder.py'}
NOMBRES = {'iforest': 'Isolation Forest', 'kmeans': 'KMeans',
           'ocsvm': 'One-Class SVM', 'dbscan': 'DBSCAN', 'autoencoder': 'Autoencoder'}

pd.set_option('display.width', 160)

# --------------------------------------------------------------------------- carga
def columna_score(df):
    for c in ('score_norm', 'score', 'error'):
        if c in df.columns:
            return c
    raise KeyError(f'sin columna de score reconocible (columnas: {list(df.columns)})')

def cargar(carpeta):
    info_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_train.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    detectores, idx_val_ref, primero = {}, None, None

    for m in MODELOS:
        # Todos los modelos guardan validación y test con el mismo formato
        ruta_ref = os.path.join(carpeta, f'{m}_scores_val.parquet')
        ruta_test = os.path.join(carpeta, f'{m}_scores_test.parquet')

        if not (os.path.exists(ruta_ref) and os.path.exists(ruta_test)):
            print(f'  {m}: sin scores de val y test en {carpeta}/, se omite '
                  f'(python pipeline/{SCRIPT_DE[m]})')
            continue

        ref_df = pd.read_parquet(ruta_ref)
        test_df = pd.read_parquet(ruta_test).reindex(info_test.index)
        
        col_ref = columna_score(ref_df)
        col_test = columna_score(test_df)
        
        if test_df[col_test].isna().any():
            raise ValueError(f'{m}: los scores de test no cuadran con info_test')

        # La comparación solo es justa si TODOS validan con las mismas filas
        if idx_val_ref is None:
            idx_val_ref, primero = ref_df.index, m
        elif not ref_df.index.equals(idx_val_ref):
            raise ValueError(f'{m}: sus filas de validación no coinciden con las de {primero}. '
                             f'Revisa que todos usen proporcion_val del config.yaml.')

        detectores[m] = {
            'ref': ref_df[col_ref].to_numpy(dtype=float),
            'test': test_df[col_test].to_numpy(dtype=float),
        }
        
    if not detectores:
        raise SystemExit(f'No hay modelos en {carpeta}/')

    grupo = COLUMNA_GRUPO if (COLUMNA_GRUPO in info_train and COLUMNA_GRUPO in info_test) else None
    
    info_val = info_train.loc[idx_val_ref]
    datos = {
        'y_val': info_val['es_fraude'].astype(int).to_numpy(),
        'grupo_val': info_val[grupo].to_numpy() if grupo else None,
        'y_test': info_test['es_fraude'].astype(int).to_numpy(),
        'tipo_test': info_test['tipo_fraude'].to_numpy(),
        'ids_test': info_test['transaccion_id'].to_numpy(),
        'grupo_test': info_test[grupo].to_numpy() if grupo else None,
        'dias_test': max((info_test['timestamp'].max() - info_test['timestamp'].min())
                         .total_seconds() / 86400, 1),
    }
    
    print(f'Modelos cargados: {", ".join(detectores)}')
    print(f'Validación: {len(datos["y_val"]):,} filas, {datos["y_val"].sum()} fraudes  (para ELEGIR)')
    print(f'Test:       {len(datos["y_test"]):,} filas, {datos["y_test"].sum()} fraudes  (para MEDIR)')
    return detectores, datos

# --------------------------------------------------------------------------- bootstrap
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

def ap_bootstrap_test(y, lista_scores, n, grupos=None, semilla=SEMILLA):
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

def bootstrap_eleccion_val(tabla, detectores, datos, n):
    # ¿El ganador lo es de verdad o es ruido? Bootstrap pareado en VALIDACIÓN,
    # que es donde se ha elegido (el test no se usa para decidir nada).
    nombres = tabla['detector'].tolist()
    aps = ap_bootstrap_test(datos['y_val'], [detectores[x]['ref'] for x in nombres], n,
                            datos['grupo_val'])
    
    filas = []
    for j, nombre in enumerate(nombres):
        dif_inf, dif_sup = np.percentile(aps[:, 0] - aps[:, j], [2.5, 97.5])
        if j == 0:
            veredicto = 'ganador'
        elif dif_inf > 0:
            veredicto = 'peor'
        elif dif_sup < 0:
            veredicto = 'mejor'
        else:
            veredicto = 'empate técnico'
            
        filas.append({'detector': nombre, 'PR-AUC_val': tabla['PR-AUC_val'].iloc[j],
                      'dif_inf': dif_inf, 'dif_sup': dif_sup, 'veredicto': veredicto})
    return pd.DataFrame(filas)

# --------------------------------------------------------------------------- evaluación
def punto_operacion(y, marcadas, dias):
    vp = int((marcadas & (y == 1)).sum())
    n = int(marcadas.sum())
    prec = vp / n if n else 0.0
    rec = vp / max(1, y.sum())
    return {'marcadas_%': marcadas.mean() * 100, 'alertas_dia': n / dias,
            'precision_%': prec * 100, 'recall_%': rec * 100,
            'f1_%': 200 * prec * rec / (prec + rec) if prec + rec else 0.0}

def evaluar_detector(nombre, det, datos):
    y_test = datos['y_test']
    s_test = det['test']
    
    # Umbral fijado en validación (no en test)
    umbral = np.quantile(det['ref'], 1 - PRESUPUESTO_PCT / 100)
    det['marca_presupuesto'] = s_test >= umbral

    pr = average_precision_score(y_test, s_test)
    fila = {'detector': nombre,
            'PR-AUC_val': average_precision_score(datos['y_val'], det['ref']),
            'PR-AUC': pr, 'ROC-AUC': roc_auc_score(y_test, s_test), 'lift': pr / y_test.mean()}
            
    for k, v in punto_operacion(y_test, det['marca_presupuesto'], datos['dias_test']).items():
        fila[f'pres_{k}'] = v
    return fila

def tabla_top_k(y, s):
    orden = np.argsort(-s)
    filas = []
    for pct in PORCENTAJES_TOP:
        n = max(1, int(round(len(s) * pct / 100)))
        vp = int(y[orden[:n]].sum())
        filas.append({'top_%': pct, 'revisadas': n, 'fraudes': vp,
                      'precision_%': round(vp / n * 100, 1),
                      'recall_%': round(vp / max(1, y.sum()) * 100, 1)})
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

def grafico_pr(detectores, datos, carpeta):
    y = datos['y_test']
    fig, ax = plt.subplots(figsize=(8, 6))
    for nombre, det in detectores.items():
        p, r, _ = precision_recall_curve(y, det['test'])
        lw = 2.5 if nombre == 'autoencoder' else 1.5
        ax.plot(r, p, lw=lw, label=f'{nombre} (PR-AUC {average_precision_score(y, det["test"]):.3f})')
        
    ax.axhline(y.mean(), ls='--', c='grey', lw=1, label=f'azar ({y.mean() * 100:.2f} %)')
    ax.set(xlabel='Recall', ylabel='Precisión', title='Curvas precisión-recall en test (Comparativa Hito 4)',
           xlim=(0, 1), ylim=(0, 1.02))
    ax.legend(fontsize=9)
    ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(os.path.join(carpeta, 'comparativa_final_pr.png'), dpi=130)
    plt.close(fig)

# --------------------------------------------------------------------------- markdown
def generar_markdown(tabla, eleccion, por_tipo):
    f = tabla.set_index('detector')
    mejor = f.index[0]
    
    L = ["Comparativa Final (Deep Learning vs Clásicos)\n"]
    L.append(f"Tras entrenar la arquitectura de Autoencoder, se evaluó su rendimiento contra "
             f"los baselines Isolation Forest, KMeans, One-Class SVM y DBSCAN.\n")
             
    L.append(f"- **El mejor modelo es `{mejor}`**: elegido por su PR-AUC en validación "
             f"({f.loc[mejor, 'PR-AUC_val']:.3f}) y medido en test ({f.loc[mejor, 'PR-AUC']:.3f}).")
    L.append("- PR-AUC en test de cada modelo: "
             + ", ".join(f"{NOMBRES.get(d, d)} {v:.3f}" for d, v in f['PR-AUC'].items()) + ".")
    if mejor == 'autoencoder':
        L.append("- El enfoque semi-supervisado con deep learning logró superar a los métodos puramente no supervisados.")
    else:
        L.append("- El Autoencoder no logró superar a los algoritmos clásicos de detección.")
    if 'autoencoder' in f.index:
        L.append("- Ojo: el Autoencoder es semi-supervisado (se entrena solo con transacciones "
                 "legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). "
                 "Los clásicos no usan etiquetas para entrenar: no es del todo igualdad de condiciones.")
        
    L.append("\n## Análisis Estadístico (Bootstrap en Validación)")
    empatan = eleccion.loc[eleccion['veredicto'] == 'empate técnico', 'detector'].tolist()
    if empatan:
        L.append(f"- Estadísticamente (IC 95%), el ganador EMPATA TÉCNICAMENTE con: {', '.join(empatan)}.")
        
    L.append(f"\n## Desempeño Operativo al {PRESUPUESTO_PCT}% de Revisión")
    rec, prec, alertas = f.loc[mejor, 'pres_recall_%'], f.loc[mejor, 'pres_precision_%'], f.loc[mejor, 'pres_alertas_dia']
    L.append(f"- El mejor modelo encuentra el {rec:.0f}% del fraude revisando solo el {PRESUPUESTO_PCT}% de las transacciones.")
    L.append(f"- La precisión es del {prec:.0f}% ({prec:.0f} de cada 100 alertas revisadas son fraude real), generando {alertas:.0f} alertas diarias.")
    
    if len(por_tipo):
        L.append('\n## Capacidades de Detección por Tipo')
        for tipo, fila in por_tipo.sort_values('n', ascending=False).iterrows():
            L.append(f"- **{tipo}** (n={int(fila['n'])}): "
                     + ", ".join(f"{NOMBRES.get(m, m)} {fila[m]:.0f}%"
                                 for m in por_tipo.columns if m != 'n') + ".")
    return L


# --------------------------------------------------------------------------- main
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
    
    filas = [evaluar_detector(n, d, datos) for n, d in detectores.items()]
    # Se ordena (y se elige) por VALIDACIÓN; el test solo mide
    tabla = pd.DataFrame(filas).sort_values('PR-AUC_val', ascending=False).reset_index(drop=True)
    mejor = tabla.loc[0, 'detector']
    
    # Calcular IC95 de Test para todos
    aps = ap_bootstrap_test(datos['y_test'], [detectores[n]['test'] for n in tabla['detector']],
                            N_BOOTSTRAP, datos['grupo_test'])
    inf, sup = np.percentile(aps, [2.5, 97.5], axis=0)
    tabla['IC95_inf'] = inf
    tabla['IC95_sup'] = sup
    
    tabla.insert(1, 'ganador', np.where(tabla['detector'] == mejor, '★', ''))
    
    seccion('COMPARATIVA FINAL  (elegido por PR-AUC de validación, medido en test)')
    cols = ['detector', 'ganador', 'PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC']
    print(tabla[cols].round(3).to_string(index=False))
    
    seccion('¿EL GANADOR GANA DE VERDAD?  (bootstrap pareado en validación)')
    eleccion = bootstrap_eleccion_val(tabla, detectores, datos, N_BOOTSTRAP)
    print(eleccion.round(4).to_string(index=False))
    
    seccion(f'RECALL % POR TIPO DE FRAUDE (presupuesto {PRESUPUESTO_PCT:g} %)')
    marcas = {n: detectores[n]['marca_presupuesto'] for n in detectores}
    por_tipo = recall_por_tipo(marcas, datos)
    print(por_tipo.to_string())

    grafico_pr(detectores, datos, CARPETA_RESULTADOS)
    
    lineas = generar_markdown(tabla, eleccion, por_tipo)
    ruta_md = os.path.join(CARPETA_RESULTADOS, 'conclusiones_hito4.md')
    with open(ruta_md, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lineas) + '\n')
    
    print(f'\n\nProceso Finalizado. Markdown generado en: {ruta_md}')

if __name__ == '__main__':
    main()