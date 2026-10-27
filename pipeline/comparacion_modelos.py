import os
import sys
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, precision_recall_curve

# Raíz del proyecto, donde está config.py
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_MODELOS, CARPETA_RESULTADOS
# Carga, métricas y bootstrap son las MISMAS funciones que usa la evaluación de los clásicos,
# así los números de esta comparativa coinciden con los de resultados/comparativa_test.csv
from evaluar_modelos_nosupervisados import (N_BOOTSTRAP, PRESUPUESTO_PCT, SUPERVISADOS, ap_bootstrap,
                                            bootstrap_eleccion, cargar,
                                            evaluar_detector, recall_por_tipo)

NOMBRES = {'iforest': 'Isolation Forest', 'kmeans': 'KMeans', 'ocsvm': 'One-Class SVM', 'dbscan': 'DBSCAN',
           'gmm': 'GMM', 'autoencoder': 'Autoencoder', 'hgb_gmm': 'HGB + GMM (supervisado)'}

pd.set_option('display.width', 160)

# --------------------------------------------------------------------------- gráfico
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
def lineas_autoencoder(f, eleccion, mejor):
    """Dónde queda el autoencoder: frente a Isolation Forest (la comparación que pide el hito),
    frente al resto en test, y si la diferencia con el ganador es significativa en validación."""
    ae = f.loc['autoencoder']
    L = []
    if 'iforest' in f.index:
        iso = f.loc['iforest']
        verbo = 'supera' if ae['PR-AUC'] > iso['PR-AUC'] else 'no supera'
        L.append(f"- Frente a Isolation Forest, el Autoencoder {verbo} en test: PR-AUC "
                 f"{ae['PR-AUC']:.3f} frente a {iso['PR-AUC']:.3f}; revisando el {PRESUPUESTO_PCT:g} % "
                 f"encuentra el {ae['pres_recall_%']:.0f} % del fraude frente al {iso['pres_recall_%']:.0f} %.")

    otros = f.drop(index=['autoencoder', *[s for s in SUPERVISADOS if s in f.index]])
    mejores = [NOMBRES.get(d, d) for d in otros.index if otros.loc[d, 'PR-AUC'] > ae['PR-AUC']]
    peores = [NOMBRES.get(d, d) for d in otros.index if otros.loc[d, 'PR-AUC'] <= ae['PR-AUC']]
    if not mejores:
        L.append("- En test el Autoencoder es el que mejor PR-AUC obtiene de los modelos no supervisados.")
    else:
        L.append(f"- En test queda por debajo de {', '.join(mejores)}"
                 + (f" y por encima de {', '.join(peores)}." if peores else "."))

    if mejor != 'autoencoder':
        veredicto = eleccion.set_index('detector')['veredicto'].get('autoencoder')
        if veredicto == 'empate':
            L.append(f"- En validación la diferencia con el ganador ({NOMBRES.get(mejor, mejor)}) "
                     f"no es significativa (IC 95 % del bootstrap): empate técnico.")
        elif veredicto == 'peor':
            L.append(f"- En validación es significativamente peor que el ganador "
                     f"({NOMBRES.get(mejor, mejor)}) según el bootstrap (IC 95 %).")
    return L


def generar_markdown(tabla, eleccion, por_tipo, mejor):
    f = tabla.set_index('detector')

    L = ["# Comparativa Final (Deep Learning vs Clásicos)\n"]
    L.append(f"Tras entrenar la arquitectura de Autoencoder, se evaluó su rendimiento contra "
             f"los baselines Isolation Forest, KMeans, One-Class SVM, DBSCAN y GMM.\n")

    L.append(f"- **El mejor modelo es `{mejor}`**: elegido por su PR-AUC en validación "
             f"({f.loc[mejor, 'PR-AUC_val']:.3f}) y medido en test ({f.loc[mejor, 'PR-AUC']:.3f}).")
    L.append("- PR-AUC en test de cada modelo: "
             + ", ".join(f"{NOMBRES.get(d, d)} {v:.3f}" for d, v in f['PR-AUC'].items()) + ".")
    if 'autoencoder' in f.index:
        L.extend(lineas_autoencoder(f, eleccion, mejor))
    if 'autoencoder' in f.index:
        L.append("- Ojo: el Autoencoder es semi-supervisado (se entrena solo con transacciones "
                 "legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). "
                 "Los clásicos no usan etiquetas para entrenar: no es del todo igualdad de condiciones.")
    for s in [s for s in SUPERVISADOS if s in f.index]:
        L.append(f"- {NOMBRES.get(s, s)} aprende con los fraudes confirmados al final del train: PR-AUC "
                 f"{f.loc[s, 'PR-AUC']:.3f} en test y, al {PRESUPUESTO_PCT:g} %, el {f.loc[s, 'pres_recall_%']:.0f} % "
                 f"del fraude. Sale en la tabla como referencia de lo que aportan las etiquetas, pero no compite: "
                 f"el ganador se elige entre los no supervisados.")

    L.append("\n## Análisis Estadístico (Bootstrap en Validación)")
    empatan = eleccion.loc[eleccion['veredicto'] == 'empate', 'detector'].tolist()
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
    # El ganador, entre los que no ven etiquetas (los supervisados salen como referencia)
    compiten = tabla[~tabla['detector'].isin(SUPERVISADOS)].reset_index(drop=True)
    mejor = compiten.loc[0, 'detector']

    # Calcular IC95 de Test para todos
    aps = ap_bootstrap(datos['y_test'], [detectores[n]['test'] for n in tabla['detector']],
                            N_BOOTSTRAP, datos['grupo_test'])
    inf, sup = np.percentile(aps, [2.5, 97.5], axis=0)
    tabla['IC95_inf'] = inf
    tabla['IC95_sup'] = sup

    tabla.insert(1, 'ganador', np.where(tabla['detector'] == mejor, '<--', ''))
    tabla.insert(2, 'tipo', np.where(tabla['detector'].isin(SUPERVISADOS), 'supervisado', 'no supervisado'))

    seccion('COMPARATIVA FINAL  (elegido por PR-AUC de validación entre los no supervisados, medido en test)')
    cols = ['detector', 'ganador', 'tipo', 'PR-AUC_val', 'PR-AUC', 'IC95_inf', 'IC95_sup', 'ROC-AUC']
    print(tabla[cols].round(3).to_string(index=False))

    seccion('¿EL GANADOR GANA DE VERDAD?  (bootstrap pareado en validación)')
    # Todos los que compiten contra el ganador (la evaluación de los clásicos solo compara los 3 primeros)
    eleccion = bootstrap_eleccion(compiten, detectores, datos, N_BOOTSTRAP, n_top=len(compiten))
    print(eleccion.round(4).to_string(index=False))
    
    seccion(f'RECALL % POR TIPO DE FRAUDE (presupuesto {PRESUPUESTO_PCT:g} %)')
    marcas = {n: detectores[n]['marca_presupuesto'] for n in detectores}
    por_tipo = recall_por_tipo(marcas, datos)
    print(por_tipo.to_string())

    grafico_pr(detectores, datos, CARPETA_RESULTADOS)
    
    lineas = generar_markdown(tabla, eleccion, por_tipo, mejor)
    ruta_md = os.path.join(CARPETA_RESULTADOS, 'conclusiones_hito4.md')
    with open(ruta_md, 'w', encoding='utf-8') as fh:
        fh.write('\n'.join(lineas) + '\n')
    
    print(f'\n\nProceso Finalizado. Markdown generado en: {ruta_md}')

if __name__ == '__main__':
    main()