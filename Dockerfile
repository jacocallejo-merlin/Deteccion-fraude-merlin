# Imagen del pipeline antifraude: Python + dependencias + código. La usan docker-compose.yml
# (servicio "pipeline") y los Job/CronJob de Kubernetes (k8s/).
# Los datos no van dentro: ClickHouse es otro contenedor y lo que generan los scripts
# (datos_ml/, modelos/, resultados/, historico/) va a un volumen.
#
#   docker build -t fraude-pipeline:latest .
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Primero las dependencias (capa que cambia poco): torch solo CPU, la de PyPI trae CUDA y ocupa varios GB
COPY requirements-pipeline.txt .
RUN pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements-pipeline.txt

# Los hilos OpenMP ociosos duermen en vez de girar en vacío. Sin esto, con la CPU compartida
# (Kubernetes, varios contenedores) el autoencoder iba unas 10 veces más lento.
# No cambia el número de hilos ni los resultados.
ENV OMP_WAIT_POLICY=PASSIVE

# Después el código
COPY config.py config.yaml main.py artefactos.py ./
COPY pipeline/ pipeline/
COPY web/ web/
COPY web_modelos/ web_modelos/

# Usuario sin privilegios, dueño de las carpetas donde escriben los scripts
RUN useradd --create-home --uid 1000 fraude \
 && mkdir -p datos_ml modelos resultados historico \
 && chown -R fraude:fraude /app
USER fraude

# Por defecto, el pipeline completo sin borrar nada (si la base de datos está vacía, genera el
# dataset). Para regenerarlo todo: python main.py --desde-cero. Ver main.py --help
CMD ["python", "main.py"]
