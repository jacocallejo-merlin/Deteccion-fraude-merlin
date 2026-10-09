# Herramienta antifraude: detección de anomalías en transacciones

Pipeline que genera un dataset sintético de pagos, lo carga en ClickHouse, calcula variables
por transacción y entrena detectores de anomalías no supervisados (Isolation Forest, KMeans,
DBSCAN, One-Class SVM) y un autoencoder. Cada transacción recibe un score de anomalía que se
guarda en la tabla `anomaly_scores`.

## Puesta en marcha

```bash
docker compose up -d                  # ClickHouse local
cp .env.example .env                  # contraseña de ClickHouse
python -m venv venv
venv/Scripts/pip install -r requirements.txt     # en Linux/Mac: venv/bin/pip
venv/Scripts/python main.py           # BORRA la base de datos y lo regenera todo
```

Opciones de `main.py`:

| Comando | Qué hace |
|---|---|
| `python main.py` | Borra la base de datos y ejecuta todos los pasos. Con la misma semilla sale siempre lo mismo. |
| `python main.py --reutilizar-datos` | No regenera el dataset; ejecuta el resto. |
| `python main.py --desde PASO` | Empieza en ese paso sin borrar nada (p. ej. `--desde modelos_nosupervisados`). |

Todos los parámetros (semilla, particiones, rejillas de hiperparámetros, autoencoder) están en
[config.yaml](config.yaml).

## Pasos del pipeline

| # | Script | Entrada | Salida |
|---|---|---|---|
| 1 | `BD_VACIAS.py` | — | Base de datos y tablas vacías |
| 2 | `dataset.py` | — | Datos sintéticos con fraude etiquetado (9 tipos) |
| 3 | `crear_tabla_enriquecida.py` | tablas origen | `transacciones_enriquecidas` |
| 4 | `features.py` | `transacciones_enriquecidas` | `features_transaccion` (una fila por transacción) |
| 5 | `reglas_alerta.py` | `features_transaccion` | Alertas por reglas |
| 6 | `informe_limpieza.py` | tablas origen | Informe de calidad de datos |
| 7 | `preparacion_datos_ml.py` | `features_transaccion` | `datos_ml/`: X/info de train y test, escalador |
| 8 | `modelos_nosupervisados.py` | `datos_ml/` | `modelos/`: modelos, scores, `resumen_entrenamiento.json`; `anomaly_scores` |
| 9 | `entrenar_autoencoder.py` | `datos_ml/` | `modelos/autoencoder.pt` y scores |
| 10 | `puntuar_nosupervisados.py` | modelos guardados | Scores de las transacciones aún sin puntuar |
| 11 | `puntuar_autoencoder.py` | autoencoder guardado | Ídem para el autoencoder |
| 12 | `evaluar_modelos_nosupervisados.py` | scores de val y test | `resultados/`: comparativa, curvas PR, conclusiones |
| 13 | `evaluar_autoencoders.py` | scores del autoencoder | Análisis del autoencoder |
| 14 | `comparacion_modelos.py` | scores de val y test | Comparativa final |
| 15 | `validacion_reproducibilidad_consistencia.py` | todo lo anterior | Informe de reproducibilidad |

Cada entrenamiento guarda una copia de sus artefactos y del `config.yaml` usado en
`historico/<run_id>/`.

## Entrenamiento y scoring de los modelos clásicos (Hito 3)

### 1. Variables (`features.py`)

Se calculan en SQL sobre ClickHouse y **solo usan información anterior a cada transacción**
(las ventanas excluyen la transacción actual o terminan en ella), para que el modelo no vea
el futuro.

| Grupo | Variables |
|---|---|
| Importe | `log_importe`, `pct_limite_credito` |
| Tiempo | hora y día de la semana (codificados con seno/coseno), `es_finde`, `es_madrugada`, días desde el alta |
| Canal y método | `es_online`, `canal_app`, `canal_web`, `es_credito`, `rechazada`, `sin_autenticacion` |
| Sesión y dispositivo | `proxy_vpn`, `num_intentos_login`, `ip_extranjera`, `dispositivo_nuevo`, `n_clientes_dispositivo`, eventos de la sesión, `min_seg_entre_eventos` |
| Velocidad de la tarjeta | pagos en 10 min / 1 h / 24 h, importe en 10 min, rechazos en 1 h, `seg_desde_anterior_tarjeta` |
| Velocidad del cliente | `n_cliente_1h` (transacciones por hora), `seg_desde_anterior_cliente`, `n_tarjetas_cliente_24h` (tarjetas distintas) |
| Historial del cliente | `z_importe_cliente` (z-score del importe frente a su historial), `ratio_importe_habitual`, `n_devoluciones_30d` |

La etiqueta (`es_fraude`, `tipo_fraude`) se guarda aparte y **nunca entra al modelo**.

### 2. Preparación (`preparacion_datos_ml.py`)

1. Se descartan los primeros 30 días (los clientes aún no tienen historial).
2. Logaritmo en variables muy asimétricas y codificación cíclica de hora y día.
3. **Partición temporal**: el 70 % más antiguo es train y el 30 % más reciente es test.
4. `StandardScaler` ajustado **solo con train** y aplicado a test.

### 3. Entrenamiento y elección de configuración (`modelos_nosupervisados.py`)

El train se divide otra vez por fecha: el 85 % más antiguo es el **ajuste** y el 15 % más
reciente es la **validación** (las mismas filas para todos los modelos).

- Cada configuración de la rejilla se entrena con el ajuste **sin etiquetas**.
- Se elige la de mayor PR-AUC en validación. Es el único uso de la etiqueta: entrenamiento no
  supervisado, selección de modelo con un conjunto de validación etiquetado.
- La configuración elegida se reentrena con 5 semillas para comprobar que el resultado no
  depende del azar.

| Modelo | Score (más alto = más anómalo) | Notas |
|---|---|---|
| Isolation Forest | `-score_samples` | Rejilla: `n_estimators`, `max_samples`, `max_features` |
| KMeans | Distancia al centroide más cercano | Rejilla: `k` |
| DBSCAN | Distancia al punto central (core point) más cercano; si es mayor que `eps`, es ruido | No tiene `predict`: se guardan sus puntos centrales. Muestra de 10.000 filas. Rejilla: `eps`, `min_samples` |
| One-Class SVM | `-decision_function` | Muestra de 10.000 filas. Rejilla: `nu`, `gamma` |

Para cada modelo se guarda en `modelos/`:

- `<modelo>.joblib`: el modelo entrenado.
- `<modelo>_referencia.joblib`: scores del ajuste, ordenados, para convertir un score en percentil.
- `<modelo>_scores_{train,val,test}.parquet`: scores en bruto y normalizados (0 = típico, 1 = percentil 99 del ajuste).
- `resumen_entrenamiento.json`: configuración elegida, PR-AUC de validación y estabilidad.

### 4. Scoring (`puntuar_nosupervisados.py`)

Puntúa con los modelos guardados todas las transacciones de `features_transaccion` que aún no
tienen score de ese modelo. Aplica exactamente la misma preparación que en el entrenamiento
(`preparar_features`: mismas variables, transformaciones y escalador, **sin reajustar nada**).

Resultado en la tabla `anomaly_scores`:

| Columna | Significado |
|---|---|
| `transaccion_id` | Transacción |
| `model` | `iforest`, `kmeans`, `dbscan`, `ocsvm` o `autoencoder` |
| `score` | Score en bruto |
| `score_pct` | Percentil frente al ajuste (0,99 = más rara que el 99 % de las transacciones de entrenamiento) |
| `run_id` | Ejecución que lo generó |

Al entrenar solo se guardan en ClickHouse los scores de test; en la primera ejecución el
scoring rellena el resto (train, validación y los días de calentamiento). Esos scores son
*dentro de muestra* para las transacciones con las que se entrenó el modelo.

### 5. Evaluación (`evaluar_modelos_nosupervisados.py`)

- Modelo elegido: el de mejor PR-AUC en **validación**. El test solo se usa para medir.
- Métricas en test: PR-AUC (con intervalo de confianza por bootstrap agrupado por cliente),
  ROC-AUC y lift.
- Puntos de operación con umbrales fijados en validación: revisar el 1 % más sospechoso y el
  umbral de F1 máximo.
- Recall por tipo de fraude.
- Salidas en `resultados/`: `comparativa_test.csv`, `bootstrap_eleccion_val.csv`,
  `recall_por_tipo_test.csv`, `curvas_pr_test.png` y `conclusiones.md`.

## Orquestación

[dags/fraude_dags.py](dags/fraude_dags.py) define tres DAGs de Airflow: `fraude_setup` (a mano),
`fraude_entrenamiento` (a mano o semanal) y `fraude_scoring` (diario).

## Carpetas

| Carpeta | Contenido |
|---|---|
| `pipeline/` | Scripts del pipeline |
| `notebooks/` | Exploración y visualizaciones |
| `datos_ml/` | Datos preparados para los modelos |
| `modelos/` | Modelos entrenados y scores |
| `resultados/` | Métricas, gráficos y conclusiones |
| `historico/` | Copia de cada ejecución |
| `legacy/` | Código antiguo que ya no se usa (ver su `LEEME.md`) |
