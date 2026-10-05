import os
import random
from pathlib import Path

import numpy as np
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

def fijar_semillas(semilla=None):
    """Fija la semilla de random, numpy y (si está instalado) PyTorch.
    Sin argumento usa la del config.yaml. Devuelve la semilla usada."""
    semilla = CONFIG['semilla'] if semilla is None else semilla
    random.seed(semilla)
    np.random.seed(semilla)
    try:
        import torch
        torch.manual_seed(semilla)
    except ImportError:
        pass   # PyTorch solo lo usa el autoencoder
    return semilla

CONFIG = cargar_config()

HOST = CONFIG['clickhouse']['host']
PORT = CONFIG['clickhouse']['port']
USER = CONFIG['clickhouse']['user']
PASSWORD = CONFIG['clickhouse']['password']
DATABASE = CONFIG['clickhouse']['database']
