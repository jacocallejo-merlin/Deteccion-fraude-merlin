#!/bin/sh
# Arranque del contenedor: ClickHouse en segundo plano, la copia de los datos dentro y las dos webs en $PORT
# (Render la fija; en Hugging Face no existe y se usa 7860).
set -e
mkdir -p /tmp/clickhouse
clickhouse server --config-file=/app/clickhouse/clickhouse.xml &
python cargar_instantanea.py
exec uvicorn unida:app --host 0.0.0.0 --port "${PORT:-7860}" --proxy-headers --forwarded-allow-ips='*'
