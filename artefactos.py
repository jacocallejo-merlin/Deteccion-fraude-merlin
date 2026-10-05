import shutil
from pathlib import Path

CARPETA = Path(__file__).resolve().parent
CARPETA_ARTEFACTOS = CARPETA / 'historico'
FICHERO_ULTIMO_RUN = CARPETA_ARTEFACTOS / 'ultimo_run.txt'


def crear_carpeta_run(run_id):
    """Crea artifacts/<run_id>/ y guarda dentro una copia del config.yaml."""
    carpeta = CARPETA_ARTEFACTOS / run_id
    carpeta.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CARPETA / 'config.yaml', carpeta / 'config.yaml')
    return carpeta


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


def marcar_ultimo_run(run_id):
    """Apunta cuál es la última ejecución, para que la evaluación sepa dónde guardar."""
    CARPETA_ARTEFACTOS.mkdir(exist_ok=True)
    FICHERO_ULTIMO_RUN.write_text(run_id, encoding='utf-8')


def ultimo_run():
    """Devuelve el run_id de la última ejecución, o None si no hay ninguna."""
    if FICHERO_ULTIMO_RUN.exists():
        return FICHERO_ULTIMO_RUN.read_text(encoding='utf-8').strip()
    return None
