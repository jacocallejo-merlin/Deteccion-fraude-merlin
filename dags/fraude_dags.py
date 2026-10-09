"""
DAGs de Airflow del proyecto antifraude 

Tres DAGs, todos lanzan los scripts de pipeline/ con BashOperator:
  fraude_setup          (manual, una vez)   borra la BD -> crea tablas -> dataset inicial
  fraude_entrenamiento  (manual o semanal)  ETL -> entrena los 5 modelos -> puntúa -> evalúa -> valida
  fraude_scoring        (periódico)         lote nuevo -> ETL -> puntúa con los modelos guardados -> valida
Los scripts de puntuar son idempotentes (solo puntúan lo pendiente), así que se pueden
reintentar sin duplicar nada. La validación sale con error si algo falla: la tarea queda en rojo.
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

# ¿El DAG de scoring genera un lote de transacciones nuevas antes de puntuar?
# True para la demo (si no, no habría nada nuevo que puntuar).
GENERAR_LOTE = True
DIAS_POR_LOTE = 2

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
        bash_command=f'cd "{RUTA_PROYECTO}" && python pipeline/{script} {argumentos}'.strip(),
    )


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
        bash_command=(f'cd "{RUTA_PROYECTO}" && python -c '
                      '"from main import preparar_base_de_datos; preparar_base_de_datos()"'),
    )
    crear_tablas = paso('crear_tablas', 'BD_VACIAS.py')
    dataset = paso('dataset_inicial', 'dataset.py')

    borrar_bd >> crear_tablas >> dataset


# -------------------------------------------------------------------- entrenamiento
with DAG(
    dag_id='fraude_entrenamiento',
    description='ETL, entrenamiento de los 5 modelos, scoring, evaluación y validación',
    schedule=None,                         # a mano; para semanal: '@weekly'
    start_date=INICIO,
    catchup=False,
    max_active_runs=1,
    default_args=ARGS,
    tags=['fraude'],
) as dag_entrenamiento:
    # ETL
    enriquecida = paso('crear_tabla_enriquecida', 'crear_tabla_enriquecida.py')
    features = paso('features', 'features.py')
    reglas = paso('reglas_alerta', 'reglas_alerta.py')
    limpieza = paso('informe_limpieza', 'informe_limpieza.py')
    preparacion = paso('preparacion_datos_ml', 'preparacion_datos_ml.py')

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

    validacion = paso('validacion', 'validacion_reproducibilidad_consistencia.py')

    enriquecida >> features >> [reglas, limpieza] >> preparacion
    preparacion >> [clasicos, autoencoder]
    clasicos >> puntuar_clasicos
    autoencoder >> puntuar_ae
    autoencoder >> eval_ae
    # Las comparativas leen los scores de los 5 modelos: esperan a los dos entrenamientos
    [clasicos, autoencoder] >> eval_clasicos
    [clasicos, autoencoder] >> comparacion
    [puntuar_clasicos, puntuar_ae, eval_clasicos, eval_ae, comparacion] >> validacion


# -------------------------------------------------------------------------- scoring
with DAG(
    dag_id='fraude_scoring',
    description='Puntúa las transacciones nuevas con los modelos ya entrenados',
    schedule='@daily',                     # cada cuánto se puntúa
    start_date=INICIO,
    catchup=False,                         # no recuperar ejecuciones pasadas
    max_active_runs=1,
    default_args=ARGS,
    tags=['fraude'],
) as dag_scoring:
    enriquecida = paso('crear_tabla_enriquecida', 'crear_tabla_enriquecida.py')
    features = paso('features', 'features.py')
    reglas = paso('reglas_alerta', 'reglas_alerta.py')
    puntuar_clasicos = paso('puntuar_nosupervisados', 'puntuar_nosupervisados.py')
    puntuar_ae = paso('puntuar_autoencoder', 'puntuar_autoencoder.py')
    validacion = paso('validacion', 'validacion_reproducibilidad_consistencia.py')

    if GENERAR_LOTE:
        lote = paso('generar_lote', 'generar_lote.py', f'--dias {DIAS_POR_LOTE}')
        lote >> enriquecida

    enriquecida >> features >> reglas >> [puntuar_clasicos, puntuar_ae] >> validacion