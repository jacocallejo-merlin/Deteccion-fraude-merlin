import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))

from config import CONFIG, DATABASE, HOST, PASSWORD, PORT, USER

CALENTAMIENTO = 30   


def preparar_base(nombre):
    import logging

    import clickhouse_connect
    logging.getLogger('clickhouse_connect').setLevel(logging.CRITICAL)
    try:
        cliente = clickhouse_connect.get_client(
            host=HOST, port=PORT, username=USER, password=PASSWORD)
        cliente.command('SELECT 1')
    except Exception as e:
        texto = str(e).lower()
        if 'authentication' in texto or 'password' in texto or 'code: 194' in texto:
            print(f'ClickHouse RESPONDE en {HOST}:{PORT}, pero rechaza las credenciales '
                  f'del usuario "{USER}".')
        else:
            print(f'No se puede conectar a ClickHouse en {HOST}:{PORT}.')
        print(f'\n  Detalle: {e}')
        sys.exit(1)

    cliente.command(f'DROP DATABASE IF EXISTS {nombre}')
    print(f'ClickHouse responde. Base de datos de pruebas "{nombre}" vaciada.')

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--semilla', type=int, default=777,
                    help='distinta de la del dataset original, o saldrían los mismos datos')
    ap.add_argument('--dias', type=int, default=75,
                    help=f'días a generar; los primeros {CALENTAMIENTO} se descartan después')
    ap.add_argument('--base', default=f'{DATABASE}_nuevo', help='base de datos de pruebas')
    ap.add_argument('--salida', default='datos_ml_nuevo')
    args = ap.parse_args()

    if not os.path.exists('datos_ml/escalador.joblib'):
        sys.exit('No está datos_ml/escalador.joblib. Este conjunto solo tiene sentido si se '
                 'escala con el escalador del entrenamiento original.')
    if args.semilla == CONFIG['semilla']:
        sys.exit(f'--semilla {args.semilla} es la misma del dataset original: saldrían '
                 f'exactamente los mismos datos y no probarías nada. Usa otra.')
    if args.dias <= CALENTAMIENTO:
        sys.exit(f'--dias {args.dias} no deja nada: los primeros {CALENTAMIENTO} días se '
                 f'descartan. Pon al menos {CALENTAMIENTO + 15}.')
    if args.base == DATABASE:
        sys.exit(f'--base {args.base} es tu base de datos principal, y este script la borra '
                 f'para empezar de cero. Usa otro nombre (por defecto {DATABASE}_nuevo).')

    pasos = [
        (['pipeline/BD_VACIAS.py'],                            'Crea las tablas vacías'),
        (['pipeline/dataset.py', '--seed', str(args.semilla),
          '--dias', str(args.dias)],                           'Genera las transacciones nuevas'),
        (['pipeline/crear_tabla_enriquecida.py'],              'Une las tablas'),
        (['pipeline/features.py'],                             'Calcula las features'),
        (['preparar_test.py',
          '--salida', args.salida],                            'Escala con el escalador original'),
    ]

    print('=' * 78)
    print(f'CONJUNTO DE PRUEBA NUEVO   (semilla {args.semilla}, {args.dias} días, '
          f'~{args.dias - CALENTAMIENTO} utilizables)')
    print('=' * 78)
    preparar_base(args.base)

    entorno = {**os.environ, 'CLICKHOUSE_DATABASE': args.base}
    inicio_total = time.perf_counter()

    for i, (comando, descripcion) in enumerate(pasos, 1):
        print('\n' + '-' * 78)
        print(f'[{i}/{len(pasos)}] {comando[0]}')
        print(f'        {descripcion}')
        print('-' * 78)
        if subprocess.run([sys.executable, *comando], cwd=CARPETA, env=entorno).returncode != 0:
            print(f'\nFALLO en {comando[0]}. Se detiene aquí.')
            sys.exit(1)

    print('\n' + '=' * 78)
    print(f'LISTO en {time.perf_counter() - inicio_total:.0f} s')
    print('=' * 78)
    print(f'Conjunto nuevo en "{args.salida}/".')


if __name__ == '__main__':
    main()