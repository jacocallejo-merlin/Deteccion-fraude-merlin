# Código obsoleto (no se usa)

Primeras versiones de los modelos, antes de reorganizar el pipeline. Ningún script de
`pipeline/` ni `main.py` los importa ni los ejecuta. Se guardan solo como historia del proyecto.

**No usar para sacar resultados:** algunos eligen el umbral mirando el test
(`entrenar_isolation_forest.py`), lo que infla las métricas (fuga de datos).

Versión actual:

| Obsoleto | Sustituido por |
|---|---|
| `feature_engineering.py` | `pipeline/features.py` |
| `entrenar_isolation_forest.py`, `entrenar_kmeans.py`, `entrenar_ocsvm.py` | `pipeline/modelos_nosupervisados.py` |
| `evaluar_modelo.py`, `evaluar_kmeans.py` | `pipeline/evaluar_modelos_nosupervisados.py` |
