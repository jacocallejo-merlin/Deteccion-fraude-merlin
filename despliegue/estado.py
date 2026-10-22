"""
El estado del scoring diario en la nube (GitHub Actions, .github/workflows/scoring-diario.yml): todo lo que el
pipeline necesita de un día para otro, guardado en la rama "estado" del repositorio de la web publicada.

    python despliegue/estado.py guardar --git URL                    # sube el estado actual (la primera vez, desde el PC)
    python despliegue/estado.py restaurar --git URL --sobrescribir   # lo recupera (lo hace GitHub Actions cada noche)

El estado es:
  - una copia completa de la base de datos de ClickHouse (todas las tablas, no solo las de la web);
  - datos_ml/ (el escalador y los conjuntos de entrenamiento y examen), modelos/ (los modelos entrenados y el
    recalibrador) y resultados/ (las métricas que se acumulan cada semana);
  - de historico/, el último run y los recalibrados de cada semana simulada. Las copias de los entrenamientos
    (clasicos_..., autoencoder_...) se quedan fuera: ocupan cientos de MB y el scoring no las lee.

"restaurar" reemplaza la base de datos de config.yaml y esas carpetas por las del estado: por eso pide --sobrescribir.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / 'despliegue'))
sys.path.insert(0, str(RAIZ / 'despliegue' / 'contenedor'))
import clickhouse_connect

from cargar_instantanea import cargar
from config import DATABASE, HOST, PASSWORD, PORT, USER
from publicar import exportar_base, subir_git, vaciar

CARPETA = RAIZ / 'despliegue' / '_estado'
RAMA = 'estado'
CARPETAS = ['datos_ml', 'modelos', 'resultados']
HISTORICO = 'historico'


def del_historico(ruta):
    """Lo que se guarda de historico/: el último run y los recalibrados semanales."""
    return ruta.name == 'ultimo_run.txt' or ruta.name.startswith('recalibrado_')


def guardar(url):
    vaciar(CARPETA)
    CARPETA.mkdir(parents=True)
    for nombre in CARPETAS:
        if not (RAIZ / nombre).exists():
            raise SystemExit(f'Falta {nombre}/. Ejecuta antes el pipeline: python main.py')
        shutil.copytree(RAIZ / nombre, CARPETA / nombre)
    (CARPETA / HISTORICO).mkdir()
    for ruta in (RAIZ / HISTORICO).iterdir() if (RAIZ / HISTORICO).exists() else []:
        if del_historico(ruta):
            (shutil.copytree if ruta.is_dir() else shutil.copy2)(ruta, CARPETA / HISTORICO / ruta.name)
    n = exportar_base(CARPETA / 'instantanea')
    megas = sum(f.stat().st_size for f in CARPETA.rglob('*') if f.is_file()) / 1e6
    print(f'Estado preparado: {n} tablas y vistas de {DATABASE} y las carpetas del pipeline, {megas:.1f} MB')
    subir_git(CARPETA, url, RAMA, 'Estado del scoring diario')
    print(f'Subido a la rama "{RAMA}".')


def restaurar(url):
    vaciar(CARPETA)
    subprocess.run(['git', 'clone', '-q', '--depth', '1', '--branch', RAMA, url, str(CARPETA)], check=True)
    for nombre in CARPETAS:
        vaciar(RAIZ / nombre)
        shutil.copytree(CARPETA / nombre, RAIZ / nombre)
    (RAIZ / HISTORICO).mkdir(exist_ok=True)
    for ruta in (CARPETA / HISTORICO).iterdir():
        destino = RAIZ / HISTORICO / ruta.name
        vaciar(destino) if destino.is_dir() else destino.unlink(missing_ok=True)
        (shutil.copytree if ruta.is_dir() else shutil.copy2)(ruta, destino)
    cliente = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD)
    cargar(cliente, CARPETA / 'instantanea', DATABASE)


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0].strip())
    p.add_argument('accion', choices=['guardar', 'restaurar'])
    p.add_argument('--git', metavar='URL', required=True, help='repositorio de la web publicada (rama "estado")')
    p.add_argument('--sobrescribir', action='store_true',
                   help='con restaurar: confirma que se reemplacen la base de datos y las carpetas del pipeline')
    args = p.parse_args()
    if args.accion == 'guardar':
        guardar(args.git)
    elif not args.sobrescribir:
        raise SystemExit(f'restaurar reemplaza la base de datos {DATABASE} y las carpetas {", ".join(CARPETAS)}: '
                         'añade --sobrescribir si es lo que quieres')
    else:
        restaurar(args.git)


if __name__ == '__main__':
    main()
