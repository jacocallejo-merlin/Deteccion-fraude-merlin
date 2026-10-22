import argparse
import subprocess
import sys
import time
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
sys.path.insert(0, str(CARPETA))

from config import DATABASE, HOST, PASSWORD, PORT, USER  

PASOS = [
    ('pipeline/crear_tablas.py',                      'Crea la base de datos y las tablas vacías'),
    ('pipeline/dataset.py',                        'Genera el dataset sintético (con errores típicos) y lo carga'),
    ('pipeline/carga_transacciones.py',            'Convierte las transacciones brutas (fechas en texto)'),
    ('pipeline/informe_limpieza.py',               'Informe de calidad de los datos de origen'),
    ('pipeline/limpieza.py',                       'Corrige duplicados y formatos inconsistentes'),
    ('pipeline/crear_tabla_enriquecida.py',        'Une las tablas en transacciones_enriquecidas'),
    ('pipeline/features.py',                       'Calcula features_transaccion'),
    ('pipeline/reglas_alerta.py',                  'Genera las alertas por reglas'),
    ('pipeline/preparacion_datos_ml.py',           'Train/test, escalado y parquets en datos_ml/'),
    ('pipeline/modelos_nosupervisados.py',         'Entrena IF, KMeans, OCSVM y DBSCAN y guarda scores'),
    ('pipeline/entrenar_autoencoder.py',           'Entrena el autoencoder y guarda scores'),
    ('pipeline/puntuar_nosupervisados.py',         'Puntúa las transacciones pendientes con los clásicos'),
    ('pipeline/puntuar_autoencoder.py',            'Puntúa las transacciones pendientes con el autoencoder'),
    ('pipeline/simular_analistas.py',              'Los analistas (simulados) revisan las alertas de cada día'),
    ('pipeline/generar_avisos.py',                 'Une modelo y reglas en una cola de alertas y crea los avisos'),
    ('pipeline/integrar_feedback.py',              'Junta el feedback con los fraudes confirmados por el banco'),
    ('pipeline/recalibrar_modelo.py',              'Recalibra el orden de las alertas si toca (cada semana)'),
    ('pipeline/medir_feedback.py',                 'Mide si las alertas mejoran semana a semana'),
    ('pipeline/ajustar_umbrales.py',               'Ajusta umbrales y variables con lo validado'),
    ('pipeline/evaluar_modelos_nosupervisados.py', 'Compara los 5 modelos y saca conclusiones'),
    ('pipeline/evaluar_autoencoders.py',           'Análisis detallado del autoencoder'),
    ('pipeline/comparacion_modelos.py',            'Comparativa final (clásicos vs autoencoder)'),
    ('pipeline/vigilar_drift.py',                  '¿Los pagos recientes se parecen a los del entrenamiento? (drift)'),
    ('pipeline/validacion_reproducibilidad_consistencia.py', 'Valida reproducibilidad y consistencia'),
]

# Pasos que crean y cargan los datos de origen: se saltan si la base de datos ya tiene datos
PASOS_DATOS = {'pipeline/crear_tablas.py', 'pipeline/dataset.py'}

# --scoring: lo que se ejecuta cada día con los modelos ya entrenados (igual que el DAG fraude_scoring)
PASOS_SCORING = [
    ('pipeline/generar_lote.py',                   'Añade los días que falten hasta hoy (normalmente uno)'),
    ('pipeline/carga_transacciones.py',            'Convierte las transacciones brutas (fechas en texto)'),
    ('pipeline/limpieza.py',                       'Corrige duplicados y formatos inconsistentes'),
    ('pipeline/crear_tabla_enriquecida.py',        'Une las tablas en transacciones_enriquecidas'),
    ('pipeline/features.py',                       'Calcula features_transaccion'),
    ('pipeline/reglas_alerta.py',                  'Genera las alertas por reglas'),
    ('pipeline/puntuar_nosupervisados.py',         'Puntúa las transacciones pendientes con los clásicos'),
    ('pipeline/puntuar_autoencoder.py',            'Puntúa las transacciones pendientes con el autoencoder'),
    ('pipeline/simular_analistas.py',              'Los analistas (simulados) revisan las alertas de cada día'),
    ('pipeline/generar_avisos.py',                 'Une modelo y reglas en una cola de alertas y crea los avisos'),
    ('pipeline/integrar_feedback.py',              'Junta el feedback con los fraudes confirmados por el banco'),
    ('pipeline/recalibrar_modelo.py',              'Recalibra el orden de las alertas si toca (cada semana)'),
    ('pipeline/medir_feedback.py',                 'Mide si las alertas mejoran semana a semana'),
    ('pipeline/vigilar_drift.py',                  '¿Los pagos recientes se parecen a los del entrenamiento? (drift)'),
    ('pipeline/validacion_reproducibilidad_consistencia.py', 'Valida reproducibilidad y consistencia'),
]


def avisar_fallo(script, args):
    """Aviso por Teams/Slack/correo (pipeline/notificar.py) de que el pipeline se ha parado. Si el
    propio aviso falla, no tapa el error original."""
    try:
        sys.path.insert(0, str(CARPETA / 'pipeline'))
        from notificar import notificar
        modo = 'scoring diario' if args.scoring else 'pipeline completo'
        notificar(f'Falla el pipeline antifraude en {Path(script).stem}',
                  f'El {modo} (python main.py) se ha parado en {script}. Mira el log de la ejecución; '
                  'los pasos anteriores sí se completaron.', 'error', tipo='fallos_pipeline')
    except Exception as e:
        print(f'(no se ha podido enviar el aviso del fallo: {e})')


def nombre_paso(script):
    return Path(script).stem


def conectar():
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
    return cliente


def dias_del_scoring(cliente):
    """Días que el scoring diario (generar_lote.py) ha añadido después del dataset original: los
    que hay hasta la última sesión de un lote completo (lo de un lote a medias no cuenta)."""
    if not cliente.command(f'EXISTS TABLE {DATABASE}.transaccion_bruta') or \
            not cliente.command(f'SELECT count() FROM {DATABASE}.transaccion_bruta'):
        return 0
    sys.path.insert(0, str(CARPETA / 'pipeline'))
    from dataset import FECHA_FIN          # el dataset original acaba justo antes de esta fecha
    ultima = cliente.command(
        f'SELECT max(timestamp) FROM {DATABASE}.sesion '
        f'WHERE sesion_id <= (SELECT max(sesion_id) FROM {DATABASE}.transaccion_bruta)')
    if isinstance(ultima, str):
        from datetime import datetime
        ultima = datetime.fromisoformat(ultima)
    return max(0, (ultima.date() - FECHA_FIN.date()).days + 1)


def preparar_base_de_datos(borrar):
    """Comprueba la conexión y, si borrar=True (--desde-cero), borra la base de datos.
    Devuelve True si quedan transacciones cargadas."""
    cliente = conectar()

    if borrar:
        cliente.command(f'DROP DATABASE IF EXISTS {DATABASE}')
        print(f'ClickHouse responde en {HOST}:{PORT}. Base de datos "{DATABASE}" borrada: se regenera todo.')
        return False

    hay_datos = bool(cliente.command(f'EXISTS TABLE {DATABASE}.transaccion')) and \
        cliente.command(f'SELECT count() FROM {DATABASE}.transaccion') > 0
    print(f'ClickHouse responde en {HOST}:{PORT}. Base de datos "{DATABASE}" '
          + ('con datos: se reutilizan, no se borra nada (para empezar de cero: --desde-cero).'
             if hay_datos else 'sin datos: se generan.'))
    return hay_datos


def elegir_pasos(args, hay_datos, dias_reconstruir=0):
    """Lista de (script, descripción, argumentos)."""
    if args.scoring:
        if not hay_datos:
            sys.exit('--scoring: la base de datos está vacía. Ejecuta antes "python main.py" completo.')
        return [(s, d, []) for s, d in PASOS_SCORING]
    pasos = [(s, d, []) for s, d in PASOS]
    if hay_datos:
        pasos = [p for p in pasos if p[0] not in PASOS_DATOS]
    elif dias_reconstruir:
        # Tras el dataset original, los mismos días que había añadido el scoring diario: cada día
        # con su semilla, así que salen idénticos
        i = [p[0] for p in pasos].index('pipeline/dataset.py') + 1
        pasos.insert(i, ('pipeline/generar_lote.py',
                         f'Reconstruye los {dias_reconstruir} días que había añadido el scoring diario',
                         ['--dias', str(dias_reconstruir)]))
    if args.desde:
        if not hay_datos:
            sys.exit('--desde: la base de datos está vacía. Ejecuta antes "python main.py" completo.')
        nombres = [nombre_paso(p[0]) for p in pasos]
        if args.desde not in nombres:
            sys.exit(f'--desde: paso desconocido "{args.desde}". Opciones: {", ".join(nombres)}')
        pasos = pasos[nombres.index(args.desde):]
    return pasos


def main():
    parser = argparse.ArgumentParser(
        description='Ejecuta el pipeline antifraude. Sin opciones NO borra nada: si la base de datos ya '
                    'tiene datos (incluidos los días que añade el scoring diario) los reutiliza y rehace el '
                    'resto; si está vacía, genera el dataset. Para regenerarlo todo: --desde-cero.')
    parser.add_argument('--desde-cero', action='store_true',
                        help='BORRA la base de datos y la regenera: el dataset original y, después, los mismos '
                             'días que había añadido el scoring diario (salen idénticos)')
    parser.add_argument('--solo-original', action='store_true',
                        help='con --desde-cero: solo el dataset original, sin los días del scoring diario')
    parser.add_argument('--reutilizar-datos', action='store_true',
                        help='ya es lo que se hace sin opciones; se mantiene por compatibilidad')
    parser.add_argument('--desde', metavar='PASO',
                        help='empieza en este paso sin borrar nada, p. ej. --desde entrenar_autoencoder')
    parser.add_argument('--scoring', action='store_true',
                        help='sin borrar ni entrenar: añade un lote nuevo, lo limpia, lo puntúa con los '
                             'modelos guardados y valida')
    args = parser.parse_args()

    faltan = [s for s, _ in PASOS + PASOS_SCORING if not (CARPETA / s).exists()]
    if faltan:
        sys.exit(f'No encuentro estos scripts en {CARPETA}: {", ".join(faltan)}')

    if args.desde_cero and (args.desde or args.scoring or args.reutilizar_datos):
        sys.exit('--desde-cero no se puede combinar con --desde, --scoring ni --reutilizar-datos')
    if args.solo_original and not args.desde_cero:
        sys.exit('--solo-original solo tiene sentido con --desde-cero')

    dias_reconstruir = 0
    if args.desde_cero and not args.solo_original:
        dias_reconstruir = dias_del_scoring(conectar())     # antes de borrar
        if dias_reconstruir:
            print(f'El scoring diario había añadido {dias_reconstruir} día/s: se reconstruyen tras el dataset.')
    hay_datos = preparar_base_de_datos(args.desde_cero)
    pasos = elegir_pasos(args, hay_datos, dias_reconstruir)

    print('=' * 78)
    total = len(PASOS_SCORING) if args.scoring else len(PASOS) + bool(dias_reconstruir)
    print(f'PIPELINE ANTIFRAUDE{" (SCORING)" if args.scoring else ""}  ({len(pasos)} de {total} pasos)')
    print('=' * 78)

    tiempos = []
    inicio_total = time.perf_counter()

    for i, (script, descripcion, argumentos) in enumerate(pasos, 1):
        print('\n' + '-' * 78)
        print(f'[{i}/{len(pasos)}] {script} {" ".join(argumentos)}'.rstrip())
        print(f'        {descripcion}')
        print('-' * 78)

        inicio = time.perf_counter()
        resultado = subprocess.run([sys.executable, script, *argumentos], cwd=CARPETA)
        tiempos.append((script, time.perf_counter() - inicio))

        if resultado.returncode != 0:
            print('\n' + '=' * 78)
            print(f'FALLO en {script}. El pipeline se detiene aquí.')
            print('=' * 78)
            avisar_fallo(script, args)
            sys.exit(1)

    print('\n' + '=' * 78)
    print('PIPELINE COMPLETADO')
    print('=' * 78)
    for script, segundos in tiempos:
        print(f'  {script:<36} {segundos:>8.1f} s')
    print(f'  {"TOTAL":<36} {time.perf_counter() - inicio_total:>8.1f} s')
    if not args.scoring:
        print('\nDatos listos en "datos_ml/", preparados para el entrenamiento.')
        print('\nModelos en "modelos/", resultados en "resultados/" y scores en anomaly_scores.')


if __name__ == '__main__':
    main()
