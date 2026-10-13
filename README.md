# Herramienta antifraude: detección de anomalías en transacciones

Pipeline que genera un dataset sintético de pagos, lo carga en ClickHouse, lo limpia, calcula
variables por transacción y entrena detectores de anomalías no supervisados (Isolation Forest,
KMeans, DBSCAN, One-Class SVM) y un autoencoder. Cada transacción recibe un score de anomalía que
se guarda en la tabla `anomaly_scores`.

**Resultado principal**: el mejor detector es **KMeans**. Revisando el 1 % de transacciones más
sospechosas (unas 7 alertas al día) encuentra el **42 % del fraude**, y 26 de cada 100 alertas son
fraude real. Detalle en [Resultados](#resultados).

## Índice

1. [Puesta en marcha](#puesta-en-marcha)
2. [Cómo está organizado](#cómo-está-organizado)
3. [Correspondencia con los hitos](#correspondencia-con-los-hitos)
4. [El dataset](#el-dataset)
5. [Calidad y limpieza de datos (Hitos 1 y 2)](#calidad-y-limpieza-de-datos-hitos-1-y-2)
6. [Exploración y estadística (Hitos 1 y 2)](#exploración-y-estadística-hitos-1-y-2)
7. [Modelos clásicos (Hito 3)](#modelos-clásicos-hito-3)
8. [Autoencoder (Hito 4)](#autoencoder-hito-4)
9. [Orquestación (Hito 5)](#orquestación-hito-5)
10. [Resultados](#resultados)
11. [Tests](#tests)
12. [Carpetas](#carpetas)
13. [Limitaciones](#limitaciones)

## Puesta en marcha

Requisitos: Docker (para ClickHouse) y Python 3.13 (es la versión con la que se ha probado).

```bash
docker compose up -d                  # ClickHouse local en los puertos 8123 y 9000
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

Si un paso falla, el pipeline se detiene ahí. Todos los parámetros (semilla, tamaño del dataset,
errores simulados, particiones, rejillas de hiperparámetros, autoencoder) están en
[config.yaml](config.yaml).

## Cómo está organizado

```
 dataset.py ──► transaccion_bruta ──► carga_transacciones.py ──► transaccion ──┐
 (datos sintéticos     (fechas en texto)       │                                │
  con errores)                                 └──► transaccion_cuarentena      │
                                                    (fechas ilegibles)          │
                                                                                ▼
          informe_limpieza.py (mide)  ──►  limpieza.py (corrige)  ──►  tablas limpias
                                                                                │
                                                                                ▼
          crear_tabla_enriquecida.py ──► features.py ──► features_transaccion ──┤
                                                                                │
                    ┌──────────────────────────┬────────────────────────────────┤
                    ▼                          ▼                                ▼
            reglas_alerta.py        preparacion_datos_ml.py              (notebooks)
              (alertas)              train / test, escalado
                                               │
                         ┌─────────────────────┴─────────────────────┐
                         ▼                                           ▼
             modelos_nosupervisados.py                     entrenar_autoencoder.py
             IForest, KMeans, DBSCAN, OCSVM                     (PyTorch)
                         │                                           │
                         └──────────► anomaly_scores ◄───────────────┘
                                            │
                                            ▼
                  evaluación, comparativa y validación ──► resultados/
```

Todo el tratamiento de datos (carga, limpieza, variables) se hace en SQL dentro de ClickHouse.
Python solo trae a memoria los datos ya preparados para entrenar.

### Pasos del pipeline

| # | Script | Entrada | Salida |
|---|---|---|---|
| 1 | `BD_VACIAS.py` | — | Base de datos y tablas vacías |
| 2 | `dataset.py` | — | Datos sintéticos con fraude etiquetado (9 tipos + 1 desconocido) y errores típicos de datos reales |
| 3 | `carga_transacciones.py` | `transaccion_bruta` (texto) | `transaccion` (tipada, en UTC) y `transaccion_cuarentena` |
| 4 | `informe_limpieza.py` | tablas origen | `resultados/informe_limpieza.txt`: calidad de los datos tal como llegan |
| 5 | `limpieza.py` | tablas origen | Tablas origen sin duplicados y con formatos unificados |
| 6 | `crear_tabla_enriquecida.py` | tablas origen | `transacciones_enriquecidas` |
| 7 | `features.py` | `transacciones_enriquecidas` | `features_transaccion` (una fila por transacción) |
| 8 | `reglas_alerta.py` | `features_transaccion` | Alertas por reglas |
| 9 | `preparacion_datos_ml.py` | `features_transaccion` | `datos_ml/`: X/info de train y test, escalador |
| 10 | `modelos_nosupervisados.py` | `datos_ml/` | `modelos/`: modelos, scores, `resumen_entrenamiento.json`; `anomaly_scores` |
| 11 | `entrenar_autoencoder.py` | `datos_ml/` | `modelos/autoencoder.pt` y scores |
| 12 | `puntuar_nosupervisados.py` | modelos guardados | Scores de las transacciones aún sin puntuar |
| 13 | `puntuar_autoencoder.py` | autoencoder guardado | Ídem para el autoencoder |
| 14 | `evaluar_modelos_nosupervisados.py` | scores de val y test | `resultados/`: comparativa, curvas PR, conclusiones |
| 15 | `evaluar_autoencoders.py` | scores del autoencoder | Análisis del autoencoder |
| 16 | `comparacion_modelos.py` | scores de val y test | Comparativa final |
| 17 | `validacion_reproducibilidad_consistencia.py` | todo lo anterior | Informe de reproducibilidad |

Cada entrenamiento guarda una copia de sus artefactos y del `config.yaml` usado en
`historico/<run_id>/`.

## Correspondencia con los hitos

| Hito | Qué se pedía | Dónde está | Estado |
|---|---|---|---|
| 1. Introducción y exploración | Base de datos ClickHouse, carga, informe de limpieza inicial, visualizaciones básicas | `BD_VACIAS.py`, `dataset.py`, `carga_transacciones.py`, `informe_limpieza.py`, `notebooks/exploracion_visualizacion.ipynb` | Hecho |
| 2. Estadística y preprocesamiento | Limpieza y normalización, outliers (Z-score, IQR, boxplots), patrones simples, dataset limpio para ML | `limpieza.py`, `notebooks/outliers_patrones_tecnicas.ipynb` (tablas en `resultados/hito2/`), `preparacion_datos_ml.py` | Hecho |
| 3. ML para anomalías | Isolation Forest, One-Class SVM, KMeans, DBSCAN; feature engineering; ajuste de hiperparámetros; scoring | `features.py`, `modelos_nosupervisados.py`, `puntuar_nosupervisados.py`, `evaluar_modelos_nosupervisados.py` | Hecho |
| 4. Autoencoders | Autoencoder, error de reconstrucción, comparación con los clásicos, scripts reproducibles | `entrenar_autoencoder.py`, `puntuar_autoencoder.py`, `evaluar_autoencoders.py`, `comparacion_modelos.py` | Hecho |
| 5. Orquestación y despliegue | DAGs, Docker, Kubernetes, ejecuciones periódicas, reproducibilidad | `dags/fraude_dags.py`, `docker-compose.yml` (solo ClickHouse), `validacion_reproducibilidad_consistencia.py` | En curso: falta contenerizar el pipeline y Kubernetes |
| 6. Feedback humano y dashboards | Feedback de analistas, ajuste de umbrales, dashboards | Tablas `alerta` y `analista` ya existen en el modelo de datos | Pendiente |

## El dataset

Los datos los genera el propio proyecto (`dataset.py`): 2.000 clientes, 30 comercios y unas 117.000
transacciones en 180 días, repartidas en 13 tablas (clientes, tarjetas, dispositivos, sesiones,
eventos, transacciones, devoluciones, alertas…). Se han hecho deliberadamente difíciles para que se
parezcan a los de un banco. Todo se configura en la sección `dataset` de `config.yaml`.

### Tipos de fraude

| Tipo | Qué es |
|---|---|
| `card_testing` | Muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona |
| `smurfing` | Una cantidad grande repartida en varias compras justo por debajo de un umbral |
| `account_takeover` | Alguien roba el acceso a la cuenta, cambia datos y compra |
| `ip_sospechosa` | Compra desde una IP de otro país, a menudo con VPN |
| `dispositivo_nuevo` | Compra desde un dispositivo que el cliente no había usado nunca |
| `pico_gasto` | Un gasto mucho mayor de lo habitual para ese cliente |
| `devolucion_abusiva` | Varias compras que se devuelven poco después |
| `bot` | Sesión automática: eventos separados por menos de unos segundos |
| `bust_out` | El cliente se porta bien un tiempo y de golpe agota el crédito |
| `cash_out_multicomercio` | **Desconocido**: ver abajo |

### Qué lo hace realista

- **Fraude escaso**: el 0,5 % de las transacciones. En tarjetas reales es aún menor; 0,5 % es un
  compromiso para que queden suficientes fraudes de cada tipo que medir.
- **Fraude desconocido** (`cash_out_multicomercio`): un patrón nuevo que solo aparece en los últimos
  40 días, dentro del test. Credenciales robadas usadas desde el propio dispositivo y país del
  cliente para comprar en varios comercios seguidos. No hay casos en entrenamiento ni variables o
  reglas pensadas para él: mide si los modelos detectan fraude que nadie había descrito.
- **Etiquetas con retraso e incompletas**: cada fraude tiene una `fecha_confirmacion` (retraso
  log-normal, mediana 12 días, como un contracargo) y un 10 % no se confirma nunca. Al entrenar
  solo cuentan como fraude los confirmados antes del final del periodo de train; el resto cuenta
  como legítimo (así se elige el modelo y se escogen las legítimas del autoencoder). El test se
  mide con la etiqueta real, como se haría cuando las etiquetas ya han madurado. La etiqueta real
  de train queda en `es_fraude_real` / `tipo_fraude_real` de `info_train.parquet`.
- **Historial antes del fraude**: los primeros 30 días no tienen fraude (`DIAS_SIN_FRAUDE`), para
  que cada cliente tenga un comportamiento normal con el que compararse. Por el mismo motivo
  `preparacion_datos_ml.py` descarta esos días. Al final del periodo se deja margen para que los
  casos largos (la devolución abusiva dura semanas) terminen dentro de los datos.
- **Errores de datos reales**: ver la sección siguiente.

## Calidad y limpieza de datos (Hitos 1 y 2)

`dataset.py` genera datos limpios y, con `suciedad.activar: true` en `config.yaml`, les añade los
errores más habituales de los datos reales antes de cargarlos (con una semilla aparte, así que el
dataset de fondo no cambia):

- **Filas duplicadas**: transacciones, sesiones y eventos que llegan dos veces (reenvíos, reingestas).
- **Mismo valor escrito de varias formas**: `"España"`, `" españa"`, `"ESPAÑA"`; `"aprobada"`/`"Aprobada "`;
  emails con mayúsculas o espacios.
- **Fechas como texto en varios formatos y zonas horarias**: `2026-03-05 11:22:33` (UTC, el
  habitual), `05/03/2026 11:22:33` (otro sistema) y `2026-03-05T12:22:33+01:00` (hora de Madrid con
  su desfase, que cambia en verano).
- **Fechas imposibles o vacías** (`31/02/2026`, mes 13, `""`): no se pueden recuperar.
- **Importes en otra divisa** (USD, GBP) en vez de euros.

Sin limpiar, estos errores rompen las variables sin dar ningún error: una sesión desde `" España"`
contaría como IP extranjera para un cliente de `"España"`, una transacción `"Rechazada "` no contaría
como rechazo, una transacción duplicada contaría dos veces en las ventanas de velocidad, un importe
en dólares se compararía con el historial en euros y una hora con otro desfase desordenaría las
ráfagas de pagos.

### Los tres pasos

1. **`carga_transacciones.py`** pasa `transaccion_bruta` (texto, tal como llega) a `transaccion`:
   reconoce cada formato de fecha y lo convierte a UTC. Las fechas ilegibles van a
   `transaccion_cuarentena` con el motivo, para revisarlas a mano. Comprueba que
   `cargadas + cuarentena = brutas`, es decir, que no se pierde ninguna fila.
2. **`informe_limpieza.py`** mide la calidad de los datos **tal como llegan, antes de limpiarlos**
   (nulos, duplicados, formatos, divisas, cuarentena, huérfanos, fechas imposibles, outliers) y la
   guarda en `resultados/informe_limpieza.txt`.
3. **`limpieza.py`** los corrige en ClickHouse:
   - Quita las filas idénticas (`OPTIMIZE ... FINAL DEDUPLICATE`). Las filas con el mismo
     identificador y contenido distinto **no** se tocan: se informan para revisarlas a mano.
   - Unifica los valores categóricos: agrupa los que solo difieren en mayúsculas o espacios y
     se queda con la forma mayoritaria. Los países se resuelven mirando a la vez `cliente.pais`,
     `comercio.pais` y `sesion.ip_pais`.
   - Pasa los importes a euros con el tipo de cambio de `config.yaml` (simplificación: un tipo fijo;
     en real sería el del día de la operación).
   - Pasa los emails a minúsculas y sin espacios.
   - Al final comprueba que no queda nada pendiente; si queda, el pipeline se para.

### Cómo leer el informe de limpieza

Cada comprobación termina con una de estas marcas:

| Marca | Significado |
|---|---|
| `OK` / `limpio` | No hay ningún problema |
| `ok (motivo)` | Hay valores nulos, pero son esperables (p. ej. `sesion_id` nulo en pagos con datáfono) |
| `REVISAR (n)` | Se han encontrado `n` filas con ese problema en los datos de origen |

`REVISAR` no es un fallo del pipeline: es lo que el informe tiene que encontrar, porque los errores
se inyectan a propósito. Los duplicados, los valores escritos de varias formas, las divisas y los
emails los corrige `limpieza.py` en el paso siguiente. Las transacciones en cuarentena no se
recuperan. El resto (por ejemplo, devoluciones mayores que la compra o importes atípicos) se
informa para que una persona decida.

La limpieza recupera el dataset de fondo salvo dos cosas, igual que pasaría con datos reales: las
transacciones en cuarentena se pierden (~0,1 %) y los importes convertidos pueden variar un céntimo
por el redondeo.

## Exploración y estadística (Hitos 1 y 2)

| Notebook | Contenido |
|---|---|
| `exploracion_visualizacion.ipynb` | Modelo de datos (13 tablas), filas por tabla, estadística descriptiva por clase, variables categóricas, correlaciones, distribución de importes, transacciones por cliente, comercio, dispositivo y tarjeta |
| `outliers_patrones_tecnicas.ipynb` | Outliers univariantes del importe (boxplots, Z-score, IQR), patrones simples de fraude (picos de gasto, ráfagas de pagos) y técnicas estadísticas de detección. Guarda sus tablas en `resultados/hito2/` para consultarlas sin ejecutarlo |
| `visualizacion_kmeans.ipynb` | Proyección en 2D de los clusters de KMeans y dónde caen los fraudes |

Los notebooks leen de ClickHouse, así que hace falta haber ejecutado antes `main.py`.

## Modelos clásicos (Hito 3)

### 1. Variables (`features.py`)

Se calculan en SQL sobre ClickHouse y **solo usan información anterior a cada transacción**
(las ventanas excluyen la transacción actual o terminan en ella), para que el modelo no vea
el futuro.

| Grupo | Variables | Ayuda a detectar |
|---|---|---|
| Importe | `log_importe`, `pct_limite_credito` | bust-out |
| Tiempo | hora y día de la semana (codificados con seno/coseno), `es_finde`, `es_madrugada`, días desde el alta | fraude nocturno |
| Canal y método | `es_online`, `canal_app`, `canal_web`, `es_credito`, `rechazada`, `sin_autenticacion` | — |
| Sesión y dispositivo | `proxy_vpn`, `num_intentos_login`, `ip_extranjera`, `dispositivo_nuevo`, `n_clientes_dispositivo`, eventos de la sesión, `min_seg_entre_eventos` | account takeover, IP sospechosa, bots |
| Velocidad de la tarjeta | pagos en 10 min / 1 h / 24 h, importe en 10 min, rechazos en 1 h, `seg_desde_anterior_tarjeta` | card testing, smurfing |
| Velocidad del cliente | `n_cliente_1h` (transacciones por hora), `seg_desde_anterior_cliente`, `n_tarjetas_cliente_24h` (tarjetas distintas) | uso de varias tarjetas seguidas |
| Historial del cliente | `z_importe_cliente` (z-score del importe frente a su historial), `ratio_importe_habitual`, `n_devoluciones_30d` | pico de gasto, devolución abusiva |

La etiqueta (`es_fraude`, `tipo_fraude`) se guarda aparte y **nunca entra al modelo**. Cuando falta
un valor se usa uno neutro en vez de NULL: un pago presencial no tiene sesión (0), la primera
transacción de una tarjeta está a "30 días" de la anterior, y con menos de 5 compras previas el
z-score vale 0.

Al terminar, `features.py` imprime una tabla de señales simples (p. ej. `n_tarjeta_10min >= 3`) con
el porcentaje de fraude entre las transacciones que la cumplen, para ver qué variables separan bien.

### 2. Reglas (`reglas_alerta.py`)

Además de los modelos, hay una regla por tipo de fraude conocido sobre esas variables (p. ej.
card testing: `n_tarjeta_10min >= 2 AND importe < 5`). Cada regla tiene una nota de riesgo y genera
alertas en la tabla `alerta`. Sirven de referencia y cubren fraudes que los detectores de
anomalías globales no ven (ver [Resultados](#resultados)).

### 3. Preparación (`preparacion_datos_ml.py`)

1. Se descartan los primeros 30 días (los clientes aún no tienen historial).
2. Logaritmo en variables muy asimétricas y codificación cíclica de hora y día.
3. **Partición temporal**: el 70 % más antiguo es train y el 30 % más reciente es test.
4. `StandardScaler` ajustado **solo con train** y aplicado a test.

### 4. Entrenamiento y elección de configuración (`modelos_nosupervisados.py`)

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

### 5. Scoring (`puntuar_nosupervisados.py`)

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

### 6. Evaluación (`evaluar_modelos_nosupervisados.py`)

- Modelo elegido: el de mejor PR-AUC en **validación**. El test solo se usa para medir.
- Métricas en test: PR-AUC (con intervalo de confianza por bootstrap agrupado por cliente),
  ROC-AUC y lift.
- Puntos de operación con umbrales fijados en validación: revisar el 1 % más sospechoso y el
  umbral de F1 máximo.
- Recall por tipo de fraude.
- Salidas en `resultados/`: `comparativa_test.csv`, `bootstrap_eleccion_val.csv`,
  `recall_por_tipo_test.csv`, `curvas_pr_test.png` y `conclusiones.md`.

## Autoencoder (Hito 4)

Implementado en PyTorch. Usa los mismos datos preparados, la misma validación y el mismo formato
de scores que los clásicos, así que se compara con ellos en igualdad de métricas.

| Script | En `main.py` | Qué hace |
|---|---|---|
| `pipeline/entrenar_autoencoder.py` | sí | Entrena la configuración de `config.yaml` (sección `autoencoder`) y guarda `modelos/autoencoder.pt`, su referencia y los scores |
| `pipeline/puntuar_autoencoder.py` | sí | Puntúa en `anomaly_scores` las transacciones pendientes (`model = 'autoencoder'`) |
| `pipeline/evaluar_autoencoders.py` | sí | Métricas del autoencoder, error por variable y `resultados/historico_autoencoders.csv` |
| `pipeline/comparacion_modelos.py` | sí | Comparativa con los clásicos: `resultados/conclusiones_hito4.md` y `comparativa_final_pr.png` |
| `pipeline/buscar_autoencoder.py` | no | Prueba la rejilla `busqueda_autoencoder` (~4 min por combinación) y apunta cada una en `resultados/busqueda_autoencoder.csv`; no toca el modelo en uso |
| `generar_test_autoencoder.py` | no | Genera un dataset nuevo (otra semilla) en la base `fraude_pagos_nuevo` y lo prepara en `datos_ml_nuevo/` con `preparar_test_autoencoder.py` |
| `evaluar_test_autoencoder.py` | no | Mide el autoencoder entrenado sobre ese dataset que no ha visto nunca |

- **Entrenamiento semi-supervisado**: solo con transacciones legítimas según la etiqueta
  *conocida* al entrenar (los fraudes aún no confirmados cuentan como legítimos, igual que en
  la elección de los clásicos). Score = error de reconstrucción (MSE medio por variable).
- **Parada temprana** con la pérdida de las legítimas de validación (sin ruido).
- **Elección de arquitectura**: igual que los clásicos, mejor PR-AUC en validación. La
  configuración elegida se reentrena con `semillas_estabilidad` para comprobar que el resultado
  no depende del azar (`modelos/autoencoder_resumen.json`).

Prueba con datos nuevos (opcional, no afecta a la base de datos principal):

```bash
python generar_test_autoencoder.py      # --semilla 777 --dias 75 por defecto
python evaluar_test_autoencoder.py
```

## Orquestación (Hito 5)

[dags/fraude_dags.py](dags/fraude_dags.py) define tres DAGs de Airflow. Cada tarea ejecuta un
script de `pipeline/` desde la raíz del proyecto.

| DAG | Cuándo | Qué hace |
|---|---|---|
| `fraude_setup` | A mano | Crea la base de datos desde cero y carga el dataset inicial |
| `fraude_entrenamiento` | A mano (o `@weekly`) | Carga, limpieza, variables, entrenamiento de los 5 modelos, scoring, evaluación y validación |
| `fraude_scoring` | `@daily` | Puntúa las transacciones nuevas con los modelos ya entrenados |

`validacion_reproducibilidad_consistencia.py` cierra cada entrenamiento comprobando que:

- están todos los artefactos (modelos, escalador, parquets);
- ClickHouse es coherente: una fila de variables por transacción, todas puntuadas por cada modelo,
  sin scores duplicados ni de transacciones inexistentes;
- volver a preparar los datos da el mismo `X_test.parquet`;
- los modelos guardados (clásicos y autoencoder) reproducen sus scores;
- reentrenar los clásicos con la misma semilla da los mismos scores.

Deja su informe en `resultados/validacion_<fecha>.txt`.

## Resultados

Medidos en test (el 30 % más reciente del periodo), con el modelo elegido en validación. Un
modelo al azar sacaría una PR-AUC de 0,007 (la proporción de fraude en test).

| Modelo | PR-AUC val | PR-AUC test | IC 95 % test | ROC-AUC | Fraude encontrado revisando el 1 % |
|---|---|---|---|---|---|
| **KMeans** (elegido) | 0,137 | **0,404** | 0,30 – 0,52 | 0,930 | 42 % |
| One-Class SVM | 0,097 | 0,366 | 0,26 – 0,48 | 0,898 | 42 % |
| DBSCAN | 0,127 | 0,327 | 0,23 – 0,43 | 0,933 | 32 % |
| Autoencoder | 0,079 | 0,225 | 0,15 – 0,34 | 0,862 | 32 % |
| Isolation Forest | 0,032 | 0,151 | 0,08 – 0,27 | 0,913 | 25 % |

- En validación, KMeans **empata** estadísticamente con DBSCAN, One-Class SVM y el autoencoder:
  no se puede asegurar que sea mejor, solo que no es peor.
- **Detecta bien**: account takeover, bust-out, dispositivo nuevo, bot y card testing (73–100 %).
- **Se le escapan**: smurfing, IP sospechosa, pico de gasto y devolución abusiva. Son fraudes que
  solo se salen de lo normal en una o dos variables, y un detector de anomalías mira todas a la
  vez. Para ellos funcionan mejor las reglas de `reglas_alerta.py`.
- **El fraude desconocido** (`cash_out_multicomercio`) no lo detecta ningún modelo (0 %): imita a
  un cliente normal. Necesita el feedback de los analistas (Hito 6).

Detalle en [resultados/conclusiones.md](resultados/conclusiones.md) y
[resultados/conclusiones_hito4.md](resultados/conclusiones_hito4.md). Las cifras cambian si se
cambia la semilla o `config.yaml`.

## Tests

```bash
venv/Scripts/python -m pytest tests
```

| Test | Qué comprueba |
|---|---|
| `test_limpieza.py` | Ensuciar y limpiar devuelve los datos originales; la forma correcta de cada valor; divisas recuperadas al céntimo; las fechas en otro formato o zona horaria son el mismo instante |
| `test_carga.py` | La conversión de fechas de `carga_transacciones.py` con casos conocidos (invierno, verano, imposibles); ninguna transacción se pierde; todo en euros. Necesita ClickHouse |
| `test_preparacion.py` | La partición train/test es temporal y sin solape; el escalador solo ve train; al entrenar solo se conocen los fraudes confirmados antes del corte |
| `test_features_sin_futuro.py` | Las variables de ventana (pagos en 10 min/1 h/24 h) solo cuentan transacciones anteriores; una fila por transacción; países normalizados. Necesita ClickHouse con el pipeline ejecutado (si no, se salta) |

## Carpetas

| Carpeta | Contenido |
|---|---|
| `pipeline/` | Scripts del pipeline |
| `notebooks/` | Exploración, outliers y visualizaciones |
| `dags/` | DAGs de Airflow |
| `datos_ml/` | Datos preparados para los modelos |
| `modelos/` | Modelos entrenados y scores |
| `resultados/` | Informe de limpieza, métricas, gráficos y conclusiones (`hito2/`: tablas del notebook de outliers) |
| `historico/` | Copia de cada ejecución |
| `legacy/` | Código antiguo que ya no se usa (ver su `LEEME.md`) |
| `tests/` | Tests automáticos (`pytest`) |

## Limitaciones

- **Los datos son sintéticos**, porque el enunciado pide generarlos. Se han hecho difíciles a
  propósito (fraude escaso, un tipo desconocido, etiquetas con retraso, errores de carga), pero las
  variables se diseñaron conociendo los 9 patrones del generador, así que en esos tipos las métricas
  son optimistas. El tipo desconocido es la mejor estimación de cómo se comportaría el sistema
  ante fraude nuevo.
- **Volumen**: 2.000 clientes y unas 117.000 transacciones. DBSCAN y One-Class SVM se entrenan con
  una muestra de 10.000 filas.
- **Pocos fraudes para elegir**: solo 26 fraudes confirmados en validación, así que la elección de
  modelo tiene bastante margen de error (de ahí los empates).
- **Errores de datos simulados**: solo los que se inyectan. Los datos reales traen otros
  (identificadores reasignados, cambios de esquema, tipos de cambio diarios) que habría que añadir
  al informe y a la limpieza.
- **Despliegue**: Docker solo levanta ClickHouse; el pipeline se ejecuta en local o desde Airflow.
  Falta contenerizarlo y desplegarlo en Kubernetes (Hito 5).
