import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

CARPETA = Path(__file__).resolve().parent
RUTA_CONFIG = CARPETA / 'config.yaml'


def cargar_config(ruta=RUTA_CONFIG):
    with open(ruta, encoding='utf-8') as fh:
        config = yaml.safe_load(fh)

    load_dotenv(CARPETA / '.env')
    password = os.getenv('CLICKHOUSE_PASSWORD')
    if password is None:
        raise SystemExit('Falta la contraseña de ClickHouse. Copia ".env.example" como ".env" '
                         'y escribe la contraseña en CLICKHOUSE_PASSWORD.')

    ch = config['clickhouse']
    ch['password'] = password
    ch['host'] = os.getenv('CLICKHOUSE_HOST', ch['host'])      # en Docker será otro host
    ch['port'] = int(os.getenv('CLICKHOUSE_PORT', ch['port']))
    return config


CONFIG = cargar_config()

HOST = CONFIG['clickhouse']['host']
PORT = CONFIG['clickhouse']['port']
USER = CONFIG['clickhouse']['user']
PASSWORD = CONFIG['clickhouse']['password']
DATABASE = CONFIG['clickhouse']['database']
