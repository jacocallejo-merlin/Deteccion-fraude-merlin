"""
Carga en ClickHouse una copia de la base de datos hecha por despliegue/publicar.py (exportar_base): una carpeta con
tablas.json (el CREATE de cada tabla y vista) y los datos de cada tabla en formato Native comprimido.

    python cargar_instantanea.py      # dentro del contenedor publicado (arrancar.sh), carpeta instantanea/

Empieza siempre de cero: borra la base de datos y la vuelve a crear, así el contenedor arranca igual cada vez.
También la usa despliegue/estado.py para recuperar el estado del scoring diario en GitHub Actions.
"""
import gzip
import json
import os
import time
from pathlib import Path

import clickhouse_connect

CARPETA = Path(__file__).resolve().parent / 'instantanea'


def conectar(intentos=60):
    """Espera a que ClickHouse acabe de arrancar (en el contenedor lo lanza arrancar.sh en segundo plano)."""
    for _ in range(intentos):
        try:
            return clickhouse_connect.get_client(host=os.getenv('CLICKHOUSE_HOST', '127.0.0.1'),
                                                 port=int(os.getenv('CLICKHOUSE_PORT', 8123)),
                                                 username=os.getenv('CLICKHOUSE_USER', 'default'),
                                                 password=os.environ['CLICKHOUSE_PASSWORD'])
        except Exception:
            time.sleep(1)
    raise SystemExit('ClickHouse no ha arrancado en un minuto')


def cargar(cliente, carpeta=CARPETA, base=None):
    """Carga la copia de `carpeta` en la base de datos `base` (por defecto, la misma de la que se sacó)."""
    manifiesto = json.loads((carpeta / 'tablas.json').read_text(encoding='utf-8'))
    origen = manifiesto['base']
    base = base or origen

    def sql(t):   # los CREATE nombran la base de origen: se cambia por la de destino
        return t['sql'].replace(f'{origen}.', f'{base}.') if base != origen else t['sql']

    cliente.command(f'DROP DATABASE IF EXISTS {base}')
    cliente.command(f'CREATE DATABASE {base}')

    tablas = [t for t in manifiesto['tablas'] if not t['vista']]
    for t in tablas:
        cliente.command(sql(t))
        datos = carpeta / f"{t['nombre']}.native.gz"
        if datos.exists():
            # sin juntar los bloques en uno grande antes de escribir: así cabe en los 512 MB del plan gratuito
            cliente.raw_insert(f"{base}.{t['nombre']}", insert_block=gzip.decompress(datos.read_bytes()), fmt='Native',
                               settings={'max_insert_block_size': 8192, 'min_insert_block_size_rows': 8192,
                                         'min_insert_block_size_bytes': 0})

    # Las vistas pueden leer de otras vistas: se reintentan hasta que existan todas aquellas de las que dependen
    pendientes = [t for t in manifiesto['tablas'] if t['vista']]
    while pendientes:
        fallidas = []
        for t in pendientes:
            try:
                cliente.command(sql(t))
            except Exception:
                fallidas.append(t)
        if len(fallidas) == len(pendientes):
            raise SystemExit('No se han podido crear las vistas: ' + ', '.join(t['nombre'] for t in fallidas))
        pendientes = fallidas

    print(f"Base de datos {base} cargada: {len(tablas)} tablas y {len(manifiesto['tablas']) - len(tablas)} vistas")


if __name__ == '__main__':
    cargar(conectar())
