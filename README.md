# Herramienta antifraude: detección de anomalías en transacciones

Pipeline que genera un dataset sintético de pagos, lo carga en ClickHouse, lo limpia, calcula
variables por transacción y entrena detectores de anomalías no supervisados (Isolation Forest,
KMeans, DBSCAN, One-Class SVM) y un autoencoder. Cada transacción recibe un score de anomalía que
se guarda en la tabla `anomaly_scores`.


## Índice

- [Herramienta antifraude: detección de anomalías en transacciones](#herramienta-antifraude-detección-de-anomalías-en-transacciones)
  - [Índice](#índice)
  - [Puesta en marcha](#puesta-en-marcha)
  - [Cómo está organizado](#cómo-está-organizado)
    - [Pasos del pipeline](#pasos-del-pipeline)
  - [Correspondencia con los hitos](#correspondencia-con-los-hitos)
  - [El dataset](#el-dataset)
    - [Tipos de fraude](#tipos-de-fraude)
    - [Qué lo hace realista](#qué-lo-hace-realista)
  - [Calidad y limpieza de datos (Hitos 1 y 2)](#calidad-y-limpieza-de-datos-hitos-1-y-2)
    - [Los tres pasos](#los-tres-pasos)
    - [Cómo leer el informe de limpieza](#cómo-leer-el-informe-de-limpieza)
  - [Exploración y estadística (Hitos 1 y 2)](#exploración-y-estadística-hitos-1-y-2)
  - [Modelos clásicos (Hito 3)](#modelos-clásicos-hito-3)
    - [1. Variables (`features.py`)](#1-variables-featurespy)
    - [2. Reglas (`reglas_alerta.py`)](#2-reglas-reglas_alertapy)
    - [3. Preparación (`preparacion_datos_ml.py`)](#3-preparación-preparacion_datos_mlpy)
    - [4. Entrenamiento y elección de configuración (`modelos_nosupervisados.py`)](#4-entrenamiento-y-elección-de-configuración-modelos_nosupervisadospy)
    - [5. Scoring (`puntuar_nosupervisados.py`)](#5-scoring-puntuar_nosupervisadospy)
    - [6. Evaluación (`evaluar_modelos_nosupervisados.py`)](#6-evaluación-evaluar_modelos_nosupervisadospy)
  - [Autoencoder (Hito 4)](#autoencoder-hito-4)
  - [Orquestación y despliegue (Hito 5)](#orquestación-y-despliegue-hito-5)
    - [Arquitectura](#arquitectura)
    - [Los tres flujos](#los-tres-flujos)
    - [Airflow](#airflow)
    - [Kubernetes](#kubernetes)
    - [Validación de reproducibilidad y consistencia](#validación-de-reproducibilidad-y-consistencia)
  - [Feedback de analistas (Hito 6)](#feedback-de-analistas-hito-6)
  - [Resultados](#resultados)
  - [Tests](#tests)
  - [Carpetas](#carpetas)
  - [Limitaciones](#limitaciones)

## Puesta en marcha

Requisitos: Docker (para ClickHouse) y Python 3.13 (es la versión con la que se ha probado).

```bash
docker compose up -d                  # ClickHouse local en los puertos 8123 y 9000
python -m venv venv
venv/Scripts/pip install -r requirements.txt    
venv/Scripts/python main.py           # BORRA la base de datos y lo regenera todo
```

Opciones de `main.py`:

| Comando | Qué hace |
|---|---|
| `python main.py` | Borra la base de datos y ejecuta todos los pasos. Con la misma semilla sale siempre lo mismo. |
| `python main.py --reutilizar-datos` | No regenera el dataset; ejecuta el resto. |
| `python main.py --desde PASO` | Empieza en ese paso sin borrar nada (p. ej. `--desde modelos_nosupervisados`). |
| `python main.py --scoring` | Sin borrar ni entrenar: añade un día de transacciones nuevas, las limpia, las puntúa con los modelos guardados y valida. |

Si un paso falla, el pipeline se detiene ahí. Todos los parámetros (semilla, tamaño del dataset,
errores simulados, particiones, rejillas de hiperparámetros, autoencoder) están en
[config.yaml](config.yaml).

Sin instalar Python, todo con Docker (los resultados quedan en las mismas carpetas):

```bash
docker compose run --rm --build pipeline                       # = python main.py
docker compose run --rm pipeline python main.py --scoring     # un día más de datos, puntuado
```

Airflow y Kubernetes: ver [Orquestación y despliegue](#orquestación-y-despliegue-hito-5).

## Cómo está organizado

Cada caja es un script; a la derecha, lo que lee (◄) y lo que escribe (►). Las tablas están en
ClickHouse y las carpetas en el disco.

```
 1. CARGA ─────────────────────────────────────────────────────────────────────────────────────
  BD_VACIAS.py ................ ► base de datos fraude_pagos con las 15 tablas vacías
        │
  dataset.py .................. ► cliente, comercio, dispositivo, cliente_dispositivo, metodo_pago,
        │                          canal_pago, sesion, evento, devolucion, analista, patron
        │                        ► transaccion_bruta   (las transacciones, con la fecha en texto)
        │                          (alerta queda vacía: la rellena reglas_alerta.py)
        │
  carga_transacciones.py ...... ◄ transaccion_bruta
        │                        ► transaccion              (fecha convertida a UTC)
        │                        ► transaccion_cuarentena   (fecha ilegible, con el motivo)

 2. LIMPIEZA ──────────────────────────────────────────────────────────────────────────────────
  informe_limpieza.py ......... ◄ las 13 tablas de origen + transaccion_cuarentena
        │                        ► resultados/informe_limpieza.txt   (calidad ANTES de limpiar)
        │
  limpieza.py ................. ◄► las 13 tablas de origen (las corrige en el sitio:
        │                          duplicados, mayúsculas/espacios, divisas, emails)

 3. VARIABLES ─────────────────────────────────────────────────────────────────────────────────
  crear_tabla_enriquecida.py .. ◄ transaccion + cliente, metodo_pago, canal_pago, comercio,
        │                          sesion, dispositivo
        │                        ► transacciones_enriquecidas   (una fila por transacción, todo unido)
        │                        ► alertas_enriquecidas   (vista: alerta + patron + analista)
        │
  features.py ................. ◄ transacciones_enriquecidas, evento, devolucion, cliente_dispositivo
        │                        ► features_transaccion   (las ~35 variables numéricas)
        │
        ├──► reglas_alerta.py ..... ◄ features_transaccion   ► alerta   (una regla por tipo de fraude)
        │
        └──► notebooks/ ........... ◄ cualquier tabla (sobre todo transaccion,
                                       transacciones_enriquecidas y features_transaccion)
                                     ► resultados/hito2/

 4. ENTRENAMIENTO ─────────────────────────────────────────────────────────────────────────────
  preparacion_datos_ml.py ..... ◄ features_transaccion
        │                        ► datos_ml/   (X_train, X_test, info_train, info_test, escalador)
        │
        ├──► modelos_nosupervisados.py ◄ datos_ml/
        │                               ► modelos/   (IForest, KMeans, DBSCAN, OCSVM + scores)
        │                               ► anomaly_scores   (scores de test)
        │
        └──► entrenar_autoencoder.py ... ◄ datos_ml/
                                        ► modelos/autoencoder.pt + scores
                                        ► anomaly_scores   (scores de test)

 5. SCORING ───────────────────────────────────────────────────────────────────────────────────
  puntuar_nosupervisados.py ... ◄ features_transaccion, modelos/   ► anomaly_scores   (las que faltan)
  puntuar_autoencoder.py ...... ◄ features_transaccion, modelos/   ► anomaly_scores   (las que faltan)

 6. FEEDBACK DE ANALISTAS (Hito 6) ────────────────────────────────────────────────────────────
  simular_analistas.py ........ ◄ anomaly_scores, features_transaccion ► feedback_analistas
  integrar_feedback.py ........ ◄ feedback_analistas, transaccion       ► etiquetas_conocidas
  recalibrar_modelo.py ........ ◄ etiquetas_conocidas, features         ► modelos/recalibrador.joblib
  medir_feedback.py ........... ◄ feedback_analistas, transaccion       ► resultados/feedback_semanal.*

 7. EVALUACIÓN ────────────────────────────────────────────────────────────────────────────────
  evaluar_modelos_nosupervisados.py ◄ modelos/, datos_ml/   ► resultados/conclusiones.md, CSV, curvas PR
  evaluar_autoencoders.py ......... ◄ modelos/, datos_ml/   ► resultados/historico_autoencoders.csv
  comparacion_modelos.py .......... ◄ modelos/, datos_ml/   ► resultados/conclusiones_hito4.md
  validacion_reproducibilidad_consistencia.py ◄ todo lo anterior ► resultados/validacion_<fecha>.txt

  Cada entrenamiento copia sus artefactos y el config.yaml usado en historico/<run_id>/
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
| 14 | `simular_analistas.py` | scores del día | `feedback_analistas`: veredicto de las alertas del 1 % más sospechoso |
| 15 | `integrar_feedback.py` | `feedback_analistas`, `transaccion` | `etiquetas_conocidas`: banco + analistas |
| 16 | `recalibrar_modelo.py` | `etiquetas_conocidas` | `modelos/recalibrador.joblib` (cada semana) |
| 17 | `medir_feedback.py` | `feedback_analistas` | `resultados/feedback_semanal.csv/png`, `conclusiones_feedback.md` |
| 18 | `evaluar_modelos_nosupervisados.py` | scores de val y test | `resultados/`: comparativa, curvas PR, conclusiones |
| 19 | `evaluar_autoencoders.py` | scores del autoencoder | Análisis del autoencoder |
| 20 | `comparacion_modelos.py` | scores de val y test | Comparativa final |
| 21 | `validacion_reproducibilidad_consistencia.py` | todo lo anterior | Informe de reproducibilidad |

Cada entrenamiento guarda una copia de sus artefactos y del `config.yaml` usado en
`historico/<run_id>/`.

## Correspondencia con los hitos

| Hito | Qué se pedía | Dónde está | Estado |
|---|---|---|---|
| 1. Introducción y exploración | Base de datos ClickHouse, carga, informe de limpieza inicial, visualizaciones básicas | `BD_VACIAS.py`, `dataset.py`, `carga_transacciones.py`, `informe_limpieza.py`, `notebooks/exploracion_visualizacion.ipynb` | Hecho |
| 2. Estadística y preprocesamiento | Limpieza y normalización, outliers (Z-score, IQR, boxplots), patrones simples, dataset limpio para ML | `limpieza.py`, `notebooks/outliers_patrones_tecnicas.ipynb` (tablas en `resultados/hito2/`), `preparacion_datos_ml.py` | Hecho |
| 3. ML para anomalías | Isolation Forest, One-Class SVM, KMeans, DBSCAN; feature engineering; ajuste de hiperparámetros; scoring | `features.py`, `modelos_nosupervisados.py`, `puntuar_nosupervisados.py`, `evaluar_modelos_nosupervisados.py` | Hecho |
| 4. Autoencoders | Autoencoder, error de reconstrucción, comparación con los clásicos, scripts reproducibles | `entrenar_autoencoder.py`, `puntuar_autoencoder.py`, `evaluar_autoencoders.py`, `comparacion_modelos.py` | Hecho |
| 5. Orquestación y despliegue | DAGs, Docker, Kubernetes, ejecuciones periódicas, reproducibilidad | `dags/fraude_dags.py`, `Dockerfile`, `airflow/Dockerfile`, `docker-compose*.yml`, `k8s/`, `generar_lote.py`, `main.py --scoring`, `validacion_reproducibilidad_consistencia.py` | Hecho |
| 6. Feedback humano y dashboards | Feedback de analistas, ajuste de umbrales, dashboards | Feedback: `simular_analistas.py`, `integrar_feedback.py`, `recalibrar_modelo.py`, `medir_feedback.py`. Umbrales y dashboard (HTML): pendientes | En curso |

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

## Orquestación y despliegue (Hito 5)

El mismo pipeline se puede ejecutar de cuatro formas. Todas lanzan los mismos scripts de
`pipeline/`, con las mismas versiones de librerías y la misma semilla:

| Forma | Para qué | Cómo |
|---|---|---|
| Local | Desarrollo | `venv/Scripts/python main.py` |
| Docker Compose | Ejecutar sin instalar Python | `docker compose run --rm pipeline` |
| Airflow | Orquestar con DAGs, reintentos e interfaz web | `docker compose -f docker-compose.yml -f docker-compose.airflow.yml up -d --build` |
| Kubernetes | Ejecución programada y escalable | `kubectl apply -k k8s/` |

### Arquitectura

```
                         ┌──────────────── imagen fraude-pipeline (Dockerfile) ───────────────┐
                         │ python:3.13-slim + requirements-pipeline.txt + torch CPU           │
                         │ config.py, config.yaml, main.py, pipeline/                          │
                         └──────────────────────────────┬─────────────────────────────────────┘
                                                        │
   main.py               (completo: BORRA y regenera)   │      ┌─────────────────────────────┐
   main.py --reutilizar-datos   (reentrenamiento)  ─────┼────► │ ClickHouse (fraude_pagos)   │
   main.py --scoring     (lote nuevo -> puntuar)        │      │ tablas de origen, features, │
                                                        │      │ alerta, anomaly_scores      │
                                                        │      └─────────────────────────────┘
                                                        ▼
                                  volumen de artefactos: datos_ml/  modelos/  resultados/  historico/
                                  (lo escribe el entrenamiento; el scoring solo lo lee)
```

- **Los datos no van en la imagen.** ClickHouse es su propio contenedor (con volumen persistente) y
  los artefactos (escalador, modelos, resultados) van a un volumen. La imagen solo lleva código y
  dependencias, así que vale para entrenar y para puntuar.
- **Versiones fijas.** `requirements-pipeline.txt` tiene las mismas versiones que el entorno local
  (Python 3.13, scikit-learn 1.9.1, pandas 3.0.6, torch 2.14.1): un `.joblib` de scikit-learn solo
  se carga de forma fiable con la versión que lo guardó. ClickHouse también va fijado (26.9).
- **Configuración por variables de entorno.** `config.py` lee `CLICKHOUSE_HOST`, `_PORT`, `_USER`,
  `_PASSWORD` y `_DATABASE`, que tienen prioridad sobre `config.yaml`. En Docker y Kubernetes el
  host es el nombre del servicio (`clickhouse`) y la contraseña viene de un Secret.

### Los tres flujos

| Flujo | `main.py` | DAG de Airflow | Kubernetes | Cuándo |
|---|---|---|---|---|
| Setup + entrenamiento | `main.py` | `fraude_setup` y luego `fraude_entrenamiento` | Job `job-pipeline-completo.yaml` | Una vez, a mano |
| Reentrenamiento | `main.py --reutilizar-datos` | `fraude_entrenamiento` | CronJob `fraude-entrenamiento` | Domingos 05:00 |
| Scoring | `main.py --scoring` | `fraude_scoring` | CronJob `fraude-scoring` | Cada día a las 03:00 |

El **scoring** hace: `generar_lote.py` (un día de transacciones nuevas) → `carga_transacciones.py`
→ `limpieza.py` → `crear_tabla_enriquecida.py` → `features.py` → `reglas_alerta.py` →
`puntuar_nosupervisados.py` y `puntuar_autoencoder.py` (solo lo pendiente, con los modelos
guardados: nunca entrena) → feedback de analistas (`simular_analistas.py` → `integrar_feedback.py`
→ `recalibrar_modelo.py` → `medir_feedback.py`) → validación.

`generar_lote.py` simula la llegada diaria de datos, ya que el dataset es sintético: crea un día de
transacciones de los mismos clientes, tarjetas y dispositivos (los reconstruye con la semilla),
con unos pocos fraudes conocidos sin confirmar todavía y los mismos errores de carga que el
dataset inicial. En producción este paso sería la ingesta real.

Todos los pasos se pueden **reintentar** sin duplicar nada: la carga, la limpieza y las variables se
recalculan enteras; el scoring solo puntúa lo que no tiene score; y `generar_lote.py` inserta
`transaccion_bruta` la última y, si un lote anterior se quedó a medias, borra sus restos y lo
vuelve a generar igual (mismo día, misma semilla).

### Airflow

[dags/fraude_dags.py](dags/fraude_dags.py) define los tres DAGs. Cada tarea es un script de
`pipeline/` (BashOperator) y se ejecuta en paralelo lo que no depende entre sí (los clásicos y el
autoencoder se entrenan a la vez). La imagen de [airflow/Dockerfile](airflow/Dockerfile) es
Airflow 3.1 con un entorno virtual aparte para el pipeline, para que sus versiones no choquen con
las de Airflow.

```bash
docker compose -f docker-compose.yml -f docker-compose.airflow.yml up -d --build
# http://localhost:8080  ->  activar y lanzar fraude_setup, luego fraude_entrenamiento;
#                            fraude_scoring queda programado a diario
```

No se deben lanzar `fraude_entrenamiento` y `fraude_scoring` a la vez: los dos reconstruyen las
tablas de ETL (cada DAG tiene `max_active_runs=1`, pero entre DAGs no hay bloqueo).

### Kubernetes

[k8s/](k8s/) tiene todo lo necesario (se aplica con Kustomize):

| Fichero | Qué crea |
|---|---|
| `namespace.yaml`, `configmap.yaml`, `secret.yaml` | Namespace `fraude`, la configuración de `config.py` y la contraseña |
| `clickhouse.yaml` | ClickHouse como StatefulSet con volumen de 5 GiB y su Service |
| `pvc-artefactos.yaml` | Volumen para `datos_ml/`, `modelos/`, `resultados/` e `historico/` |
| `job-pipeline-completo.yaml` | Job: `python main.py` (no está en la kustomization: se lanza a mano) |
| `cronjob-scoring.yaml` | CronJob diario: `python main.py --scoring` |
| `cronjob-entrenamiento.yaml` | CronJob semanal: `python main.py --reutilizar-datos` |

Cada pod del pipeline tiene un initContainer que prepara las carpetas del volumen y espera a que
ClickHouse responda. Los CronJobs usan `concurrencyPolicy: Forbid` y horarios separados.

```bash
docker build -t fraude-pipeline:latest .
kind create cluster --name fraude                       # o Docker Desktop / minikube
kind load docker-image fraude-pipeline:latest --name fraude
kubectl apply -k k8s/
kubectl create -f k8s/job-pipeline-completo.yaml        # primera ejecución
kubectl -n fraude logs -f job/<nombre-del-job>
kubectl -n fraude create job --from=cronjob/fraude-scoring scoring-manual   # scoring sin esperar
```

En un clúster remoto la imagen se sube a un registro (y se cambia `images` en
`k8s/kustomization.yaml`), la contraseña se crea como Secret fuera del repositorio y, si hay
varios nodos, el volumen de artefactos necesita una StorageClass ReadWriteMany.

### Validación de reproducibilidad y consistencia

`validacion_reproducibilidad_consistencia.py` cierra cada entrenamiento y cada scoring (si falla,
la tarea o el Job terminan en error) comprobando que:

- están todos los artefactos (modelos, escalador, parquets);
- ClickHouse es coherente: una fila de variables por transacción, todas puntuadas por cada modelo,
  sin scores duplicados ni de transacciones inexistentes;
- volver a preparar los datos da el mismo `X_test.parquet` (también después de añadir lotes: las
  variables solo miran al pasado);
- los modelos guardados (clásicos y autoencoder) reproducen sus scores;
- con `--reentrenar` (en el DAG de entrenamiento): reentrenar los clásicos con la misma semilla da
  los mismos scores.

Deja su informe en `resultados/validacion_<fecha>.txt`.

### Prueba del despliegue

Probado en un clúster local de un nodo (kind) con `kubectl apply -k k8s/`:

- **Job del pipeline completo**: los 17 pasos en 8-16 minutos según la carga del equipo (el
  autoencoder es más de la mitad) y validación "todo correcto".
- **CronJob de scoring lanzado a mano tres veces**: cada ejecución añade un día (01, 02 y 03/09,
  ~600-670 transacciones), lo limpia y lo puntúa con los 5 modelos en menos de 1 minuto. La
  validación da "todo correcto" en las tres: todas las transacciones tienen score de cada modelo,
  sin duplicados, y las variables y scores de test no cambian al añadir datos. Repetir un día da
  exactamente el mismo lote.

Dos problemas que aparecieron en estas pruebas y están corregidos:

- `features.py` ordenaba algunas ventanas solo por `timestamp`: con dos transacciones (o eventos
  de un bot) en el mismo segundo, el orden del empate podía cambiar entre ejecuciones y con él
  variables como el tiempo desde la transacción anterior. Ahora desempata por id.
- Los hilos OpenMP ociosos giraban en vacío y, con la CPU compartida, el autoencoder iba unas 10
  veces más lento en el contenedor: las imágenes fijan `OMP_WAIT_POLICY=PASSIVE`.

El CronJob de scoring no tiene reintentos (`backoffLimit: 0`): repetir `main.py --scoring` entero
generaría otro día de datos. Si falla, el del día siguiente puntúa también lo pendiente.
- **Airflow**: la imagen carga los tres DAGs sin errores, con las mismas versiones de librerías
  que el entorno local.

## Feedback de analistas (Hito 6)

KMeans marca lo **raro**, no lo que es fraude. El feedback de los analistas cierra el ciclo: cada
día revisan las alertas, sus veredictos se juntan con los contracargos del banco y cada semana se
recalibra el orden de las alertas con lo aprendido.

| Script | Qué hace |
|---|---|
| `simular_analistas.py` | Al cerrar cada día, los analistas revisan el 1 % de transacciones con más puntuación (~7). Como no hay analistas reales, el veredicto es la etiqueta real con algo de error (8 % junior, 2 % senior) y llega entre 2 h y 2 días después. Guarda `feedback_analistas`. Si hay varios días pendientes (la primera vez, todo el test) los recorre en orden y recalibra cada semana por el camino. |
| `integrar_feedback.py` | Junta lo que se sabía en un momento dado: fraudes confirmados por el banco (con ~12 días de retraso) y veredictos de los analistas. Si se contradicen manda el banco. Deja `etiquetas_conocidas`. |
| `recalibrar_modelo.py` | Cada 7 días (o si se ha reentrenado KMeans) entrena un HistGradientBoosting con las etiquetas conocidas: entran el percentil de KMeans y las mismas variables que los modelos. Las transacciones revisadas pesan 10 veces más: enseñan qué alertas fueron falsas alarmas. Desde entonces las alertas se ordenan con él. |
| `medir_feedback.py` | Precisión y recall de la cola de alertas semana a semana, comparados en los mismos días con KMeans sin recalibrar y con un recalibrado que solo usa los contracargos del banco. |

Tabla `feedback_analistas`: `transaccion_id`, `fecha_alerta`, `fecha_revision`, `analista_id`
(tabla `analista`), `veredicto` (`fraude` / `legitima`), `tipo_fraude`, `comentario`, y además
`puntuacion` y `modelo` (con qué score y qué modelo entró la alerta en la cola).

Todo es reproducible (semilla por día) y se puede repetir sin duplicar: solo se revisan los días
puntuados que aún no tienen feedback. Si se regenera la base de datos, se borra el recalibrador
anterior y se empieza otra vez con KMeans.

Resultado en el periodo de test (6 semanas y media, ~7 alertas al día):

- Semana 1 (KMeans, sin feedback todavía): el 26 % de las alertas son fraude. Semana 6
  (recalibrado): el 52 %. Pero cada semana tiene una mezcla de fraude distinta; la comparación
  justa es en los mismos días:

| Semanas 2 a 7 (mismos días y presupuesto) | Fraudes en la cola | Precisión | Recall |
|---|---|---|---|
| KMeans sin recalibrar | 70 | 28 % | 40 % |
| Recalibrado solo con el banco | 98 | 39 % | 56 % |
| Recalibrado con banco + analistas | 95 | 38 % | 54 % |

- **Recalibrar con etiquetas mejora claramente** las alertas (+40 % de fraude encontrado con el
  mismo trabajo).
- **El feedback de los analistas no añade nada medible** sobre los contracargos del banco: aquí el
  banco confirma el 90 % del fraude y hay cientos de casos, y ~7 revisiones al día pesan poco. Donde
  es imprescindible es en los lotes diarios nuevos: el banco aún no ha confirmado nada y los
  veredictos de los analistas son las únicas etiquetas recientes.
- Detalle en [resultados/conclusiones_feedback.md](resultados/conclusiones_feedback.md) y
  `resultados/feedback_semanal.png`.

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
| `airflow/` | Imagen de Airflow con el entorno del pipeline |
| `k8s/` | Manifiestos de Kubernetes (Kustomize) |
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
- **Despliegue**: probado en un clúster local de un nodo (kind). Airflow corre en Docker Compose
  con LocalExecutor y lanza los scripts en su propio contenedor; en un clúster real lo natural
  sería Airflow con su chart de Helm y KubernetesPodOperator con la imagen `fraude-pipeline`.
  El volumen de artefactos es ReadWriteOnce (un solo nodo).
- **Lotes nuevos simulados**: `generar_lote.py` sustituye a una ingesta real y no añade el fraude
  desconocido ni casos que duran semanas (bust-out, devolución abusiva).
