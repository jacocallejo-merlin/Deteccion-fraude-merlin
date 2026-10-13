# Airflow 3 + un entorno virtual con las dependencias del pipeline.
# Los DAGs (dags/fraude_dags.py) lanzan cada script con /opt/venv_proyecto/bin/python, así que
# las versiones del pipeline (pandas, scikit-learn, torch...) no se mezclan con las de Airflow.
# Python 3.10, igual que el entorno local. Se construye desde la raíz del proyecto
# (ver docker-compose.airflow.yml) para usar el mismo requirements-pipeline.txt que el Dockerfile.
FROM apache/airflow:3.0.6-python3.10

USER root
COPY requirements-pipeline.txt /tmp/requirements-pipeline.txt
RUN python -m venv /opt/venv_proyecto \
 && /opt/venv_proyecto/bin/pip install --no-cache-dir --upgrade pip \
 # torch solo CPU: la versión por defecto de PyPI trae CUDA y ocupa varios GB
 && /opt/venv_proyecto/bin/pip install --no-cache-dir torch==2.14.1 \
        --index-url https://download.pytorch.org/whl/cpu \
 && /opt/venv_proyecto/bin/pip install --no-cache-dir -r /tmp/requirements-pipeline.txt
USER airflow
