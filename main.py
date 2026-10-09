import argparse
import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))

from config import DATABASE, HOST, PASSWORD, PORT, USER  

PASOS = [
    ('pipeline/BD_VACIAS.py',                      'Crea la base de datos y las tablas vacías'),
    ('pipeline/dataset.py',                        'Genera el dataset sintético y lo carga'),
    ('pipeline/crear_tabla_enriquecida.py',        'Une las tablas en transacciones_enriquecidas'),
    ('pipeline/features.py',                       'Calcula features_transaccion'),
    ('pipeline/reglas_alerta.py',                  'Genera las alertas por reglas'),
    ('pipeline/informe_limpieza.py',               'Informe de calidad de datos'),
    ('pipeline/preparacion_datos_ml.py',           'Train/test, escalado y parquets en datos_ml/'),
    ('pipeline/modelos_nosupervisados.py',         'Entrena IF, KMeans, OCSVM y DBSCAN y guarda scores'),
    ('pipeline/entrenar_autoencoder.py',           'Entrena el autoencoder y guarda scores'),
    ('pipeline/puntuar_nosupervisados.py',         'Puntúa las transacciones pendientes con los clásicos'),
    ('pipeline/puntuar_autoencoder.py',            'Puntúa las transacciones pendientes con el autoencoder'),
    ('pipeline/evaluar_modelos_nosupervisados.py', 'Compara los 5 modelos y saca conclusiones'),
    ('pipeline/evaluar_autoencoders.py',           'Análisis detallado del autoencoder'),
    ('pipeline/comparacion_modelos.py',            'Comparativa final (clásicos vs autoencoder)'),
    ('pipeline/validacion_reproducibilidad_consistencia.py', 'Valida reproducibilidad y consistencia'),
]

# Pasos que crean y cargan los datos de origen: se saltan con --reutilizar-datos
PASOS_DATOS = {'pipeline/BD_VACIAS.py', 'pipeline/dataset.py'}


def nombre_paso(script):
    return Path(script).stem


def preparar_base_de_datos(borrar):
    """Comprueba la conexión y, si borrar=True (lo normal), borra la base de datos.
    Devuelve True si quedan transacciones cargadas."""
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

    if borrar:
        cliente.command(f'DROP DATABASE IF EXISTS {DATABASE}')
        print(f'ClickHouse responde en {HOST}:{PORT}. Base de datos "{DATABASE}" borrada: se regenera todo.')
        return False

    hay_datos = bool(cliente.command(f'EXISTS TABLE {DATABASE}.transaccion')) and \
        cliente.command(f'SELECT count() FROM {DATABASE}.transaccion') > 0
    print(f'ClickHouse responde en {HOST}:{PORT}. Base de datos "{DATABASE}" '
          + ('con datos: se reutilizan.' if hay_datos else 'sin datos: se generan.'))
    return hay_datos


def elegir_pasos(args, hay_datos):
    pasos = PASOS
    if hay_datos and args.reutilizar_datos:
        pasos = [p for p in pasos if p[0] not in PASOS_DATOS]
    if args.desde:
        if not hay_datos:
            sys.exit('--desde: la base de datos está vacía. Ejecuta antes "python main.py" completo.')
        nombres = [nombre_paso(s) for s, _ in pasos]
        if args.desde not in nombres:
            sys.exit(f'--desde: paso desconocido "{args.desde}". Opciones: {", ".join(nombres)}')
        pasos = pasos[nombres.index(args.desde):]
    return pasos


def main():
    parser = argparse.ArgumentParser(
        description='Ejecuta el pipeline antifraude. Sin opciones BORRA la base de datos y lo regenera todo '
                    '(con la misma semilla sale siempre el mismo resultado).')
    parser.add_argument('--reutilizar-datos', action='store_true',
                        help='no borra la base de datos ni regenera el dataset; ejecuta el resto de pasos')
    parser.add_argument('--desde', metavar='PASO',
                        help='empieza en este paso sin borrar nada, p. ej. --desde entrenar_autoencoder')
    args = parser.parse_args()

    faltan = [s for s, _ in PASOS if not (CARPETA / s).exists()]
    if faltan:
        sys.exit(f'No encuentro estos scripts en {CARPETA}: {", ".join(faltan)}')

    borrar = not (args.reutilizar_datos or args.desde)
    hay_datos = preparar_base_de_datos(borrar)
    pasos = elegir_pasos(args, hay_datos)

    print('=' * 78)
    print(f'PIPELINE ANTIFRAUDE  ({len(pasos)} de {len(PASOS)} pasos)')
    print('=' * 78)

    tiempos = []
    inicio_total = time.perf_counter()

    for i, (script, descripcion) in enumerate(pasos, 1):
        print('\n' + '-' * 78)
        print(f'[{i}/{len(pasos)}] {script}')
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
        print(f'  {script:<36} {segundos:>8.1f} s')
    print(f'  {"TOTAL":<36} {time.perf_counter() - inicio_total:>8.1f} s')
    print('\nDatos listos en "datos_ml/", preparados para el entrenamiento.')
    print('\nModelos en "modelos/", resultados en "resultados/" y scores en anomaly_scores.')


if __name__ == '__main__':
    main()
