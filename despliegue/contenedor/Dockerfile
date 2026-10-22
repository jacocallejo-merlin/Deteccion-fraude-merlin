# Centinela publicado en internet: un solo contenedor con ClickHouse y las dos webs (casos en /, modelos en
# /modelos/), en el puerto $PORT (Render) o 7860 (Hugging Face Spaces). Lo arma despliegue/publicar.py junto con
# el código y una copia de los datos; no se construye desde la raíz del repositorio.
FROM clickhouse/clickhouse-server:26.9 AS clickhouse

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ClickHouse es un único binario: se copia de su imagen oficial, la misma versión que docker-compose.yml
COPY --from=clickhouse /usr/bin/clickhouse /usr/bin/clickhouse

# Solo lo que usan las webs (sin PyTorch ni scikit-learn: aquí no se entrena ni se puntúa)
COPY requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Spaces ejecuta el contenedor con el usuario 1000
RUN useradd -m -u 1000 centinela
WORKDIR /app
COPY --chown=1000:1000 . .
# Si se publicó desde un Windows que guarda los archivos con CRLF, el script no arrancaría en Linux
RUN sed -i 's/\r$//' arrancar.sh
USER centinela

# ClickHouse solo escucha dentro del contenedor (127.0.0.1): la contraseña no sale de aquí
ENV CLICKHOUSE_HOST=127.0.0.1 \
    CLICKHOUSE_PORT=8123 \
    CLICKHOUSE_USER=default \
    CLICKHOUSE_PASSWORD=centinela-local \
    CLICKHOUSE_WATCHDOG_ENABLE=0 \
    WEB_MODELOS_URL=/modelos/ \
    WEB_CASOS_URL=/

EXPOSE 7860
CMD ["sh", "arrancar.sh"]
