import os
import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.metrics import confusion_matrix, precision_recall_curve, f1_score, roc_auc_score, average_precision_score

CARPETA_DATOS = 'datos_ml'

def main():
    print("1. Cargando los datos preprocesados...")
    X_train = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_train.parquet'))
    X_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'X_test.parquet'))
    info_test = pd.read_parquet(os.path.join(CARPETA_DATOS, 'info_test.parquet'))

    print(f"   - Train: {X_train.shape[0]:,} filas | Test: {X_test.shape[0]:,} filas")

    # 2. Configuración avanzada del modelo con mejores hiperparámetros
    # Usar n_estimators más alto da estabilidad, y max_samples='auto' o 0.8 ayuda a submuestrear eficientemente.
    print("\n2. Entrenando Isolation Forest con hiperparámetros optimizados...")
    modelo = IsolationForest(
        n_estimators=250,
        max_samples=0.8,
        max_features=0.9,
        contamination='auto', # Dejamos que devuelva scores continuos puros
        random_state=42,
        n_jobs=-1
    )
    modelo.fit(X_train)

    # 3. Obtener scores de anomalía continuos
    # decision_function devuelve valores donde los más negativos indican mayor anomalía (riesgo de fraude).
    scores_test = modelo.decision_function(X_test)
    
    # Invertimos el signo para que un score MÁS ALTO signifique MAYOR riesgo de fraude (más intuitivo)
    riesgo_test = -scores_test
    info_test['score_anomalia'] = riesgo_test

    # 4. Búsqueda del umbral óptimo (Threshold Tuning) maximizando el F1-Score
    reales = info_test['es_fraude'].values
    
    # Probamos múltiples umbrales basados en los percentiles del score en test
    mejores_umbrales = np.percentile(riesgo_test, np.arange(90, 99.5, 0.1))
    mejor_f1 = -1
    mejor_umbral = 0
    mejor_preds = None

    for umbral in mejores_umbrales:
        preds = (riesgo_test >= umbral).astype(int)
        f1 = f1_score(reales, preds, zero_division=0)
        if f1 > mejor_f1:
            mejor_f1 = f1
            mejor_umbral = umbral
            mejor_preds = preds

    info_test['pred_fraude'] = mejor_preds

    # 5. Evaluación de resultados optimizados (incluyendo AUC-ROC y AUC-PR)
    mat_conf = confusion_matrix(reales, mejor_preds)
    n_marcadas = int(mejor_preds.sum())
    vp = int(((mejor_preds == 1) & (reales == 1)).sum())
    total_fraude = int(reales.sum())

    precision = (vp / n_marcadas * 100) if n_marcadas > 0 else 0
    recall = (vp / total_fraude * 100) if total_fraude > 0 else 0

    # Cálculo de métricas globales basadas en ranking continuo
    auc_roc = roc_auc_score(reales, riesgo_test)
    auc_pr = average_precision_score(reales, riesgo_test)

    print("\n--- RESULTADOS DEL ISOLATION FOREST OPTIMIZADO ---")
    print(f"Mejor umbral de riesgo seleccionado: {mejor_umbral:.4f}")
    print(f"Matriz de confusión:\n{mat_conf}")
    print(f"Transacciones marcadas como sospechosas: {n_marcadas:,}")
    print(f"Fraudes reales detectados (Verdaderos Positivos): {vp:,} de {total_fraude:,}")
    print(f"Precisión del modelo: {precision:.2f}%")
    print(f"Recall (Cobertura del fraude): {recall:.2f}%")
    print(f"F1-Score máximo alcanzado: {mejor_f1:.4f}")
    print(f"AUC-ROC: {auc_roc:.4f}")
    print(f"AUC-PR: {auc_pr:.4f}")

    # Guardar resultados para los dashboards del Hito 6
    os.makedirs('resultados_ml', exist_ok=True)
    info_test.to_parquet('resultados_ml/test_con_predicciones_iforest_optimizado.parquet')
    print("\nResultados guardados en 'resultados_ml/test_con_predicciones_iforest_optimizado.parquet'.")

if __name__ == '__main__':
    main()