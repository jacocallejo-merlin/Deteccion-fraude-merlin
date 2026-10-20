import re
import shutil
from pathlib import Path

from config import CONFIG

CARPETA = Path(__file__).resolve().parent
CARPETA_ARTEFACTOS = CARPETA / 'historico'
FICHERO_ULTIMO_RUN = CARPETA_ARTEFACTOS / 'ultimo_run.txt'
# Copias que se guardan de cada tipo de entrenamiento (clasicos_..., autoencoder_...). Cada una de
# clasicos ocupa ~10 MB y antes se acumulaban sin límite (450 MB con 44 copias)
COPIAS_POR_TIPO = CONFIG.get('historico', {}).get('copias_por_tipo', 5)
# <tipo>_AAAAMMDD_HHMMSS: las copias de cada ejecución. Las recalibrado_AAAAMMDD (una por semana
# simulada) no siguen este patrón y no se borran: son la historia de la simulación
PATRON_RUN = re.compile(r'^(?P<tipo>.+)_\d{8}_\d{6}$')


def crear_carpeta_run(run_id):
    """Crea historico/<run_id>/ y guarda dentro una copia del config.yaml. Después borra las copias
    más antiguas del mismo tipo para que queden COPIAS_POR_TIPO."""
    carpeta = CARPETA_ARTEFACTOS / run_id
    carpeta.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CARPETA / 'config.yaml', carpeta / 'config.yaml')
    limpiar_antiguos(run_id)
    return carpeta


def limpiar_antiguos(run_id):
    """Borra las copias del mismo tipo que run_id salvo las COPIAS_POR_TIPO más recientes
    (el nombre lleva la fecha, así que el orden alfabético es el cronológico). Nunca borra
    run_id ni la última ejecución apuntada en ultimo_run.txt."""
    encaje = PATRON_RUN.match(run_id)
    if not encaje or COPIAS_POR_TIPO <= 0:
        return
    tipo = encaje['tipo']
    copias = sorted(c for c in CARPETA_ARTEFACTOS.iterdir()
                    if c.is_dir() and (m := PATRON_RUN.match(c.name)) and m['tipo'] == tipo)
    protegidas = {run_id, ultimo_run()}
    for carpeta in copias[:-COPIAS_POR_TIPO]:
        if carpeta.name not in protegidas:
            shutil.rmtree(carpeta)
            print(f'  historico: borrada la copia antigua {carpeta.name} '
                  f'(se guardan las {COPIAS_POR_TIPO} últimas de {tipo})')


def guardar(origen, run_id, subcarpeta=None):
    """Copia un fichero o una carpeta entera a artifacts/<run_id>/ (o a una subcarpeta)."""
    origen = Path(origen)
    destino = CARPETA_ARTEFACTOS / run_id
    if subcarpeta:
        destino = destino / subcarpeta
    destino.mkdir(parents=True, exist_ok=True)
    if origen.is_dir():
        shutil.copytree(origen, destino / origen.name, dirs_exist_ok=True)
    elif origen.exists():
        shutil.copy2(origen, destino / origen.name)
    else:
        print(f'  (aviso) no existe {origen}, no se copia a los artefactos')


def limpiar_ficheros(carpeta, patron, mantener):
    """Borra los ficheros de carpeta que encajan con patron (glob, p. ej. 'validacion_*.txt') salvo
    los `mantener` más recientes (el nombre lleva la fecha: el orden alfabético es el cronológico)."""
    ficheros = sorted(Path(carpeta).glob(patron))
    for fichero in ficheros[:-mantener] if mantener > 0 else []:
        fichero.unlink()
    return max(0, len(ficheros) - mantener) if mantener > 0 else 0


def marcar_ultimo_run(run_id):
    """Apunta cuál es la última ejecución, para que la evaluación sepa dónde guardar."""
    CARPETA_ARTEFACTOS.mkdir(exist_ok=True)
    FICHERO_ULTIMO_RUN.write_text(run_id, encoding='utf-8')


def ultimo_run():
    """Devuelve el run_id de la última ejecución, o None si no hay ninguna."""
    if FICHERO_ULTIMO_RUN.exists():
        return FICHERO_ULTIMO_RUN.read_text(encoding='utf-8').strip()
    return None
