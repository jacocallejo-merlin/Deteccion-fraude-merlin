from datetime import datetime, timedelta
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG(
    dag_id="prueba_error",
    start_date=datetime(2026, 10, 1),
    #schedule=None,          # de momento solo manual
    schedule="*/5 * * * *",   # cada 5 minutos
    catchup=False,
    tags=["pruebas"],
    default_args={"retries": 2, "retry_delay": timedeelta(seconds=30)},
) as dag:

    tarea_ok = BashOperator(
        task_id="tarea_ok",
        bash_command="echo 'Hola, todo bien'",
    )

    tarea_falla = BashOperator(task_id="tarea_falla", bash_command="echo 'Ahora funciono' && exit 0")

    tarea_despues = BashOperator(
        task_id="tarea_despues",
        bash_command="echo 'Esto nunca debería ejecutarse'",
    )

    tarea_ok >> tarea_falla >> tarea_despues