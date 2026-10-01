"""
Pipeline completo del proyecto antifraude.

    python main.py

Borra la base de datos y la reconstruye entera: crea las tablas, genera y carga el
dataset, monta la tabla enriquecida y las features, lanza las reglas de alerta, saca
el informe de calidad y deja los parquets listos en datos_ml/.

Se borra la base de datos al empezar porque BD_VACIAS.py usa CREATE TABLE IF NOT
EXISTS, que no modifica una tabla que ya existe: sin el borrado, un cambio de esquema
se quedaría sin aplicar y el error saldría mucho más tarde, en features.py o en el
JOIN, con un mensaje que no apunta a la causa.
"""

import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))

from BD_VACIAS import DATABASE, HOST, PASSWORD, PORT, USER   # datos de conexión, sin duplicarlos

PASOS = [
    ('BD_VACIAS.py',               'Crea la base de datos y las tablas vacías'),
    ('dataset.py',                 'Genera el dataset sintético y lo carga'),
    ('crear_tabla_enriquecida.py', 'Une las tablas en transacciones_enriquecidas'),
    ('features.py',                'Calcula features_transaccion'),
    ('reglas_alerta.py',           'Genera las alertas por reglas'),
    ('informe_limpieza.py',        'Informe de calidad de datos'),
    ('preparacion_datos_ml.py',    'Train/test, escalado y parquets en datos_ml/'),
]


def preparar_base_de_datos():
    """Comprueba que el servidor responde y deja la base de datos vacía."""
    import logging

    import clickhouse_connect
    logging.getLogger('clickhouse_connect').setLevel(logging.CRITICAL)
    try:
        cliente = clickhouse_connect.get_client(
            host=HOST, port=PORT, username=USER, password=PASSWORD)
        cliente.command('SELECT 1')
    except Exception as e:
        print(f'No se puede conectar a ClickHouse en {HOST}:{PORT}.')
        print('  Levanta el contenedor con:  docker compose up -d')
        print(f'  Detalle: {e}')
        sys.exit(1)

    cliente.command(f'DROP DATABASE IF EXISTS {DATABASE}')
    print(f'ClickHouse responde en {HOST}:{PORT}. Base de datos "{DATABASE}" borrada.')


def main():
    faltan = [s for s, _ in PASOS if not (CARPETA / s).exists()]
    if faltan:
        sys.exit(f'No encuentro estos scripts en {CARPETA}: {", ".join(faltan)}')

    print('=' * 78)
    print(f'PIPELINE ANTIFRAUDE  ({len(PASOS)} pasos)')
    print('=' * 78)
    preparar_base_de_datos()

    tiempos = []
    inicio_total = time.perf_counter()

    for i, (script, descripcion) in enumerate(PASOS, 1):
        print('\n' + '-' * 78)
        print(f'[{i}/{len(PASOS)}] {script}')
        print(f'        {descripcion}')
        print('-' * 78)

        inicio = time.perf_counter()
        resultado = subprocess.run([sys.executable, script], cwd=CARPETA)
        tiempos.append((script, time.perf_counter() - inicio))

        if resultado.returncode != 0:
            print('\n' + '=' * 78)
            print(f'FALLO en {script}. El pipeline se detiene aquí.')
            print('=' * 78)
            sys.exit(1)

    print('\n' + '=' * 78)
    print('PIPELINE COMPLETADO')
    print('=' * 78)
    for script, segundos in tiempos:
        print(f'  {script:<30} {segundos:>8.1f} s')
    print(f'  {"TOTAL":<30} {time.perf_counter() - inicio_total:>8.1f} s')
    print('\nDatos listos en "datos_ml/", preparados para el entrenamiento.')


if __name__ == '__main__':
    main()
