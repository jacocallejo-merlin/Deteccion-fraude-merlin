"""
DAGs de Airflow del proyecto antifraude (Hito 5)

Tres DAGs, todos lanzan los scripts de pipeline/ con BashOperator:
  fraude_setup          (manual, una vez)   borra la BD -> crea tablas -> dataset inicial
  fraude_entrenamiento  (manual o semanal)  ETL -> entrena los 5 modelos -> puntúa -> evalúa -> valida
  fraude_scoring        (diario)            lote nuevo -> ETL -> puntúa con los modelos guardados -> valida
Los dos últimos terminan con el feedback de analistas (Hito 6):
  puntuar -> simular_analistas -> integrar_feedback -> recalibrar_modelo -> medir_feedback
Son los mismos pasos que main.py (completo y --scoring), pero en paralelo donde se puede.
Los scripts de puntuar son idempotentes (solo puntúan lo pendiente) y generar_lote.py borra los
restos de un lote a medias, así que las tareas se pueden reintentar sin duplicar nada.
La validación sale con error si algo falla: la tarea queda en rojo.

No lanzar fraude_entrenamiento y fraude_scoring a la vez: los dos reconstruyen las tablas de ETL.
Por eso tienen horas distintas (las mismas que los CronJob de k8s/): scoring a las 03:00 y
entrenamiento los domingos a las 05:00. Con '@daily' y '@weekly' coincidirían cada domingo a las 00:00.
"""
import os
from datetime import datetime, timedelta

# Airflow 3
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator
# Airflow 2 (si la versión instalada es la 2.x, usar estos en su lugar):
# from airflow import DAG
# from airflow.operators.bash import BashOperator

# Carpeta del proyecto DENTRO de donde corre Airflow (en Docker, donde se monte el proyecto)
RUTA_PROYECTO = os.environ.get('RUTA_PROYECTO', '/opt/proyecto')
# Python con las dependencias del pipeline. En la imagen de airflow/Dockerfile es un entorno
# virtual aparte, para que las versiones del pipeline no choquen con las de Airflow
PYTHON = os.environ.get('PYTHON_PROYECTO', 'python')

# ¿El DAG de scoring genera un lote de transacciones nuevas antes de puntuar?
# True para la demo (si no, no habría nada nuevo que puntuar).
GENERAR_LOTE = True
DIAS_POR_LOTE = 1   # el DAG corre a diario: cada ejecución añade un día de transacciones

ARGS = {
    'owner': 'equipo-fraude',
    'retries': 1,                         # seguro: los scripts no duplican al repetirse
    'retry_delay': timedelta(minutes=2),
}
INICIO = datetime(2026, 10, 1)


def paso(nombre, script, argumentos=''):
    """Una tarea = un script de pipeline/, ejecutado desde la raíz del proyecto."""
    return BashOperator(
        task_id=nombre,
        bash_command=f'cd "{RUTA_PROYECTO}" && {PYTHON} pipeline/{script} {argumentos}'.strip(),
    )


def etl():
    """Tareas comunes a entrenamiento y scoring: de transaccion_bruta a features_transaccion y alertas.
    Devuelve (primera, features, reglas) para engancharlas."""
    carga = paso('carga_transacciones', 'carga_transacciones.py')
    limpieza = paso('limpieza', 'limpieza.py')
    enriquecida = paso('crear_tabla_enriquecida', 'crear_tabla_enriquecida.py')
    features = paso('features', 'features.py')
    reglas = paso('reglas_alerta', 'reglas_alerta.py')
    carga >> limpieza >> enriquecida >> features >> reglas
    return carga, limpieza, features, reglas


def feedback(ajustar=False):
    """Hito 6: los analistas revisan las alertas puntuadas -> se juntan sus veredictos con los
    contracargos del banco -> se recalibra el orden de las alertas (solo si toca) -> se mide.
    ajustar=True añade el ajuste de umbrales y variables con lo validado (solo en el entrenamiento:
    es una decisión periódica, no diaria). Devuelve la primera tarea para engancharla."""
    analistas = paso('simular_analistas', 'simular_analistas.py')
    integrar = paso('integrar_feedback', 'integrar_feedback.py')
    recalibrar = paso('recalibrar_modelo', 'recalibrar_modelo.py')
    medir = paso('medir_feedback', 'medir_feedback.py')
    analistas >> integrar >> recalibrar >> medir
    if ajustar:
        medir >> paso('ajustar_umbrales', 'ajustar_umbrales.py')
    return analistas


# --------------------------------------------------------------------------- setup
with DAG(
    dag_id='fraude_setup',
    description='Crea la base de datos desde cero y carga el dataset inicial',
    schedule=None,                         # solo a mano
    start_date=INICIO,
    catchup=False,
    max_active_runs=1,
    default_args=ARGS,
    tags=['fraude'],
) as dag_setup:
    # Borra la BD entera (igual que main.py): sin esto, anomaly_scores conservaría
    # scores del dataset anterior con los mismos ids
    borrar_bd = BashOperator(
        task_id='borrar_bd',
        bash_command=(f'cd "{RUTA_PROYECTO}" && {PYTHON} -c '
                      '"from main import preparar_base_de_datos; preparar_base_de_datos(borrar=True)"'),
    )
    crear_tablas = paso('crear_tablas', 'BD_VACIAS.py')
    dataset = paso('dataset_inicial', 'dataset.py')

    borrar_bd >> crear_tablas >> dataset


# -------------------------------------------------------------------- entrenamiento
with DAG(
    dag_id='fraude_entrenamiento',
    description='ETL, entrenamiento de los 5 modelos, scoring, evaluación y validación',
    schedule='0 5 * * 0',                  # domingos a las 05:00, como el CronJob de k8s (None = solo a mano)
    start_date=INICIO,
    catchup=False,
    max_active_runs=1,
    default_args=ARGS,
    tags=['fraude'],
) as dag_entrenamiento:
    # ETL. El informe de calidad mira los datos de origen: antes de limpiarlos
    carga, limpieza, features, reglas = etl()
    informe = paso('informe_limpieza', 'informe_limpieza.py')
    carga >> informe >> limpieza
    preparacion = paso('preparacion_datos_ml', 'preparacion_datos_ml.py')
    features >> preparacion

    # Entrenamiento (en paralelo)
    clasicos = paso('entrenar_nosupervisados', 'modelos_nosupervisados.py')
    autoencoder = paso('entrenar_autoencoder', 'entrenar_autoencoder.py')

    # Scoring de todo lo que quede sin puntuar, con los modelos recién entrenados
    puntuar_clasicos = paso('puntuar_nosupervisados', 'puntuar_nosupervisados.py')
    puntuar_ae = paso('puntuar_autoencoder', 'puntuar_autoencoder.py')

    # Evaluación
    eval_clasicos = paso('evaluar_modelos_nosupervisados', 'evaluar_modelos_nosupervisados.py')
    eval_ae = paso('evaluar_autoencoders', 'evaluar_autoencoders.py')
    comparacion = paso('comparacion_modelos', 'comparacion_modelos.py')

    # Tras entrenar también se comprueba que reentrenar con la misma semilla da lo mismo
    validacion = paso('validacion', 'validacion_reproducibilidad_consistencia.py', '--reentrenar')

    preparacion >> [clasicos, autoencoder]
    clasicos >> puntuar_clasicos
    autoencoder >> puntuar_ae
    autoencoder >> eval_ae
    # Las comparativas leen los scores de los 5 modelos y las alertas de las reglas
    [clasicos, autoencoder, reglas] >> eval_clasicos
    [clasicos, autoencoder, reglas] >> comparacion
    [puntuar_clasicos, puntuar_ae, eval_clasicos, eval_ae, comparacion] >> validacion

    # Con el detector reentrenado el recalibrador anterior ya no vale: se rehace con el feedback
    analistas = feedback(ajustar=True)
    puntuar_clasicos >> analistas


# -------------------------------------------------------------------------- scoring
with DAG(
    dag_id='fraude_scoring',
    description='Puntúa las transacciones nuevas con los modelos ya entrenados',
    schedule='0 3 * * *',                  # cada día a las 03:00 (no coincide con el entrenamiento)
    start_date=INICIO,
    catchup=False,                         # no recuperar ejecuciones pasadas
    max_active_runs=1,
    default_args=ARGS,
    tags=['fraude'],
) as dag_scoring:
    carga, limpieza, features, reglas = etl()
    puntuar_clasicos = paso('puntuar_nosupervisados', 'puntuar_nosupervisados.py')
    puntuar_ae = paso('puntuar_autoencoder', 'puntuar_autoencoder.py')
    validacion = paso('validacion', 'validacion_reproducibilidad_consistencia.py')

    if GENERAR_LOTE:
        lote = paso('generar_lote', 'generar_lote.py', f'--dias {DIAS_POR_LOTE}')
        lote >> carga

    features >> [puntuar_clasicos, puntuar_ae]
    [reglas, puntuar_clasicos, puntuar_ae] >> validacion

    # scoring -> feedback -> recalibración
    analistas = feedback()
    puntuar_clasicos >> analistas
