import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from sklearn.metrics import average_precision_score, confusion_matrix, roc_auc_score

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))                 
sys.path.insert(0, str(RAIZ / 'pipeline'))   

from entrenar_autoencoder import Autoencoder, error_reconstruccion

CARPETA_NUEVO = 'datos_ml_nuevo'
CARPETA_MODELOS = 'modelos'
CARPETA_DATOS = 'datos_ml'
PRESUPUESTO_PCT = 1.0
PRESUPUESTOS_EXTRA = [0.5, 2, 5]

pd.set_option('display.width', 160)


def cargar_modelo():
    ruta = os.path.join(CARPETA_MODELOS, 'autoencoder.pt')
    if not os.path.exists(ruta):
        raise SystemExit(f'No está {ruta}. Entrena antes con pipeline/entrenar_autoencoder.py')
    g = torch.load(ruta, weights_only=False)
    modelo = Autoencoder(g['n_entrada'], g['capas'], g['cuello'])
    modelo.load_state_dict(g['estado'])
    modelo.eval()
    arq = ' -> '.join(str(t) for t in [g['n_entrada'], *g['capas'], g['cuello'],
                                       *g['capas'][::-1], g['n_entrada']])
    return modelo, g, arq


def cargar_nuevo(carpeta):
    rx = os.path.join(carpeta, 'X_nuevo.parquet')
    if not os.path.exists(rx):
        raise SystemExit(f'No está {rx}. Genera el conjunto antes con '
                         f'python generar_test_nuevo.py')
    X = pd.read_parquet(rx)
    info = pd.read_parquet(os.path.join(carpeta, 'info_nuevo.parquet'))
    tipo = (info['tipo_fraude'].astype(object).fillna('-').to_numpy()
            if 'tipo_fraude' in info.columns else None)
    return X, info['es_fraude'].astype(int).to_numpy(), tipo


def umbral_de_validacion(pct):
    ruta = os.path.join(CARPETA_MODELOS, 'autoencoder_scores_val.parquet')
    if not os.path.exists(ruta):
        raise SystemExit(f'No está {ruta}. Hace falta para fijar el umbral sin mirar los '
                         f'datos nuevos. Reentrena con pipeline/entrenar_autoencoder.py.')
    ref = pd.read_parquet(ruta)
    col = 'error' if 'error' in ref.columns else ref.columns[0]
    return float(np.quantile(ref[col].to_numpy(dtype=float), 1 - pct / 100)), len(ref)


def clasificacion(y, marca):
    vn, fp, fn, vp = confusion_matrix(y, marca, labels=[0, 1]).ravel()
    precision = vp / (vp + fp) if vp + fp else 0.0
    recall = vp / (vp + fn) if vp + fn else 0.0
    return {
        'verdaderos_positivos': int(vp), 'falsos_positivos': int(fp),
        'falsos_negativos': int(fn), 'verdaderos_negativos': int(vn),
        'precision_%': round(precision * 100, 1),
        'recall_%': round(recall * 100, 1),
        'F1_%': round(200 * precision * recall / (precision + recall), 1)
        if precision + recall else 0.0,
        'especificidad_%': round(vn / (vn + fp) * 100, 2) if vn + fp else 0.0,
        'marcadas_%': round(marca.mean() * 100, 2),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--carpeta', default=CARPETA_NUEVO)
    ap.add_argument('--presupuesto', type=float, default=PRESUPUESTO_PCT,
                    help='%% de transacciones que se revisarían (fija el umbral en validación)')
    args = ap.parse_args()

    modelo, guardado, arq = cargar_modelo()
    X, y, tipo = cargar_nuevo(args.carpeta)

    if list(X.columns) != list(guardado.get('columnas', X.columns)):
        raise SystemExit('Las columnas del conjunto nuevo no coinciden con las del modelo. '
                         'Vuelve a generarlo con generar_test_nuevo.py.')

    error = error_reconstruccion(modelo, X)
    umbral, n_val = umbral_de_validacion(args.presupuesto)
    marca = error >= umbral

    print('=' * 84)
    print('EL AUTOENCODER ENTRENADO, SOBRE DATOS QUE NO HA VISTO NUNCA')
    print('=' * 84)
    print(f'Modelo:   {arq}   ruido {guardado["ruido"]}')
    print(f'Conjunto: {len(X):,} transacciones, {int(y.sum())} fraudes '
          f'({y.mean() * 100:.2f} %)')
    print(f'Umbral:   {umbral:.4f}, fijado al {args.presupuesto:g} % sobre los {n_val:,} casos '
          f'de validación (NO sobre estos datos)')

    print('\n' + '=' * 84)
    print('1. CLASIFICACIÓN FRAUDE / NO FRAUDE')
    print('=' * 84)
    res = clasificacion(y, marca)
    tabla = pd.DataFrame([[res['verdaderos_negativos'], res['falsos_positivos']],
                          [res['falsos_negativos'], res['verdaderos_positivos']]],
                         index=['real: legítima', 'real: FRAUDE'],
                         columns=['predicho: legítima', 'predicho: FRAUDE'])
    print('\n' + tabla.to_string())
    print('\n' + pd.Series({k: v for k, v in res.items()
                            if k.endswith('%')}).to_string())
    print(f'\n  precisión {res["precision_%"]:.1f} %: de cada 100 alertas, '
          f'{res["precision_%"]:.0f} son fraude real.')
    print(f'  recall {res["recall_%"]:.1f} %: de todo el fraude que hay, se pilla esa parte.')
    print(f'  No se reporta accuracy: diciendo "nada es fraude" saldría un '
          f'{(1 - y.mean()) * 100:.2f} % sin detectar ni uno.')

    if abs(res['marcadas_%'] - args.presupuesto) > args.presupuesto * 0.5:
        print(f'\n  OJO: el umbral marca el {res["marcadas_%"]:.2f} % en vez del '
              f'{args.presupuesto:g} % previsto. Los errores de este conjunto se mueven')
        print('  respecto a validación, así que en producción el umbral habría que '
              'recalibrarlo cada cierto tiempo.')
    else:
        print(f'\n  El umbral marca el {res["marcadas_%"]:.2f} %, cerca del '
              f'{args.presupuesto:g} % previsto: transfiere bien a datos nuevos.')

    print('\n' + '=' * 84)
    print('2. CALIDAD DEL ORDEN, INDEPENDIENTE DEL UMBRAL')
    print('=' * 84)
    pr = average_precision_score(y, error)
    print(f'\n  PR-AUC   {pr:.4f}   (un score al azar sacaría {y.mean():.4f})')
    print(f'  ROC-AUC  {roc_auc_score(y, error):.4f}   (sale optimista con clases '
          f'desbalanceadas; manda la PR-AUC)')
    print(f'  lift     {pr / y.mean():.1f}x mejor que el azar')

    ruta_test = os.path.join(CARPETA_MODELOS, 'autoencoder_scores_test.parquet')
    ruta_info = os.path.join(CARPETA_DATOS, 'info_test.parquet')
    if os.path.exists(ruta_test) and os.path.exists(ruta_info):
        st = pd.read_parquet(ruta_test)
        it = pd.read_parquet(ruta_info)
        col = 'error' if 'error' in st.columns else st.columns[0]
        y_test_orig = it['es_fraude'].astype(int).to_numpy()
        pr_test = average_precision_score(y_test_orig, st[col].to_numpy(dtype=float))
        base_test = y_test_orig.mean()

        print(f'\n  Para comparar, en el test original: PR-AUC {pr_test:.4f} '
              f'(tasa de fraude {base_test * 100:.2f} %)')
        print(f'  Aquí:                               PR-AUC {pr:.4f} '
              f'(tasa de fraude {y.mean() * 100:.2f} %)')

        roc = roc_auc_score(y, error)
        roc_test = roc_auc_score(y_test_orig, st[col].to_numpy(dtype=float))
        distintas = abs(y.mean() - base_test) / max(base_test, 1e-9) > 0.15

        if distintas:
            print(f'\n  Las tasas de fraude se diferencian bastante, así que estas dos '
                  f'PR-AUC NO son comparables entre sí, y dividirlas por su tasa base '
                  f'tampoco lo arregla (corrige de más).')
            print(f'  Para saber si el modelo se comporta igual hay que mirar la ROC-AUC, '
                  f'que no depende de cuánto fraude haya:')
            print(f'     test original: {roc_test:.4f}')
            print(f'     aquí:          {roc:.4f}')
            caida, referencia = roc_test - roc, roc_test
        else:
            caida, referencia = pr_test - pr, pr_test

        relativa = caida / max(referencia, 1e-9)
        if abs(relativa) < 0.05:
            print('\n  Se mantiene. El modelo no dependía de los clientes concretos del '
                  'dataset original: ha aprendido cómo son las transacciones normales.')
        elif relativa > 0:
            print(f'\n  Baja un {relativa * 100:.0f} %. Parte de lo que medía el test '
                  'original era particular de aquellos datos.')
        else:
            print(f'\n  Sube un {-relativa * 100:.0f} %. No quiere decir que el modelo '
                  'mejore: lo más probable es que este conjunto traiga un reparto de tipos '
                  'de fraude más fácil. Mira la tabla por tipo.')

        if distintas:
            print(f'\n  Para el informe, la cifra honesta de este conjunto es su propia '
                  f'PR-AUC ({pr:.4f}) dicha junto a su tasa base ({y.mean() * 100:.2f} %), '
                  f'nunca comparada a pelo con la del test original.')

    print('\n' + '=' * 84)
    print('3. CON OTROS PRESUPUESTOS DE REVISIÓN')
    print('=' * 84)
    filas = []
    for pct in sorted({args.presupuesto, *PRESUPUESTOS_EXTRA}):
        u, _ = umbral_de_validacion(pct)
        r = clasificacion(y, error >= u)
        filas.append({'presupuesto_%': pct, 'marcadas_%': r['marcadas_%'],
                      'alertas': r['verdaderos_positivos'] + r['falsos_positivos'],
                      'fraudes_pillados': r['verdaderos_positivos'],
                      'precision_%': r['precision_%'], 'recall_%': r['recall_%'],
                      'F1_%': r['F1_%']})
    print('\n' + pd.DataFrame(filas).to_string(index=False))
    print('\n  Subir el presupuesto siempre sube el recall y baja la precisión. Dónde')
    print('  ponerse depende de cuánto cuesta revisar frente a cuánto cuesta no detectar.')

    if tipo is not None:
        print('\n' + '=' * 84)
        print(f'4. QUÉ TIPOS DE FRAUDE PILLA   (presupuesto {args.presupuesto:g} %)')
        print('=' * 84)
        fraude = y == 1
        total = pd.Series(tipo[fraude]).value_counts()
        pillados = pd.Series(tipo[fraude & marca]).value_counts()
        det = pd.DataFrame({'casos': total})
        det['pillados'] = pillados.reindex(total.index).fillna(0).astype(int)
        det['recall_%'] = (det['pillados'] / det['casos'] * 100).round(1)
        print('\n' + det.sort_values('recall_%', ascending=False).to_string())


if __name__ == '__main__':
    main()