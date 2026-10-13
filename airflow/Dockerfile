# Airflow 3 + un entorno virtual con las dependencias del pipeline.
# Los DAGs (dags/fraude_dags.py) lanzan cada script con /opt/venv_proyecto/bin/python, así que
# las versiones del pipeline (pandas, scikit-learn, torch...) no se mezclan con las de Airflow.
# Python 3.13, igual que el entorno local y la imagen del pipeline (los modelos guardados
# solo se cargan bien con las mismas versiones). Se construye desde la raíz del proyecto
# (ver docker-compose.airflow.yml) para usar el mismo requirements-pipeline.txt que el Dockerfile.
FROM apache/airflow:3.1.3-python3.13

USER root
COPY requirements-pipeline.txt /tmp/requirements-pipeline.txt
RUN python -m venv /opt/venv_proyecto \
 && /opt/venv_proyecto/bin/pip install --no-cache-dir --upgrade pip \
 # torch solo CPU: la versión por defecto de PyPI trae CUDA y ocupa varios GB
 && /opt/venv_proyecto/bin/pip install --no-cache-dir torch==2.14.1 \
        --index-url https://download.pytorch.org/whl/cpu \
 && /opt/venv_proyecto/bin/pip install --no-cache-dir -r /tmp/requirements-pipeline.txt
# Los hilos OpenMP ociosos duermen en vez de girar en vacío (ver el Dockerfile de la raíz)
ENV OMP_WAIT_POLICY=PASSIVE
USER airflow
