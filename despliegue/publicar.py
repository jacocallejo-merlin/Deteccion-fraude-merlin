"""
Publica Centinela en internet: las dos webs en un solo contenedor, con una copia de los datos actuales.

    python despliegue/publicar.py                                  # solo prepara la carpeta despliegue/_contenedor
    python despliegue/publicar.py --git https://github.com/<usuario>/centinela-web.git      # Render (gratis)
    python despliegue/publicar.py --huggingface <usuario>/centinela                         # Hugging Face (plan PRO)

Arma un contenedor Docker (plantilla en despliegue/contenedor/) con lo justo para las dos webs: su código, las cifras
de la web de modelos ya calculadas (web_modelos/publicado.json) y una copia de la base de datos de ClickHouse (la
que esté en config.yaml), que el contenedor carga al arrancar. No entrena ni puntúa: enseña los datos tal y como están ahora.
Para publicar datos nuevos después de una ejecución del pipeline, basta con volver a lanzar este script.

--git sube la carpeta a un repositorio de GitHub aparte (vacío la primera vez; se reescribe en cada publicación)
que Render construye con su render.yaml. --huggingface necesita antes `hf auth login` con un token de escritura.
"""
import argparse
import gzip
import json
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
import clickhouse_connect

from config import DATABASE, HOST, PASSWORD, PORT, USER

PLANTILLA = RAIZ / 'despliegue' / 'contenedor'
SALIDA = RAIZ / 'despliegue' / '_contenedor'

# Lo que necesitan las webs, relativo a la raíz del proyecto
CODIGO = ['config.py', 'config.yaml', 'pipeline/generar_avisos.py', 'pipeline/notificar.py',
          'web', 'web_modelos/app.py', 'web_modelos/static']
# Las tablas que consultan las webs (web/app.py y generar_avisos.py) y las que leen sus vistas. Las de entrenamiento
# (anomaly_scores, evento, sesion...) no se suben: no hacen falta y no cabrían en la memoria del plan gratuito.
# Nunca salen del PC: los ajustes de avisos (web/ajustes.py) guardan el enlace de Teams/Slack y los correos
PRIVADAS = {'ajustes'}
TABLAS = {'alerta', 'analista', 'aviso', 'aviso_leido', 'cliente', 'comercio', 'features_transaccion',
          'feedback_analistas', 'metodo_pago', 'patron', 'revision_web', 'riesgo_transaccion', 'transaccion',
          'transacciones_enriquecidas'}


def copiar(relativa):
    origen, destino = RAIZ / relativa, SALIDA / relativa
    destino.parent.mkdir(parents=True, exist_ok=True)
    if origen.is_dir():
        shutil.copytree(origen, destino, ignore=shutil.ignore_patterns('__pycache__'))
    else:
        shutil.copy2(origen, destino)


def exportar_base(carpeta, solo=None):
    """Copia de la base de datos en `carpeta`: de cada tabla (solo las de `solo`, si se da), su CREATE y sus filas en
    formato Native comprimido; de las vistas, el CREATE. La carga despliegue/contenedor/cargar_instantanea.py."""
    cliente = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD)
    filas = cliente.query('SELECT name, engine, create_table_query FROM system.tables '
                          'WHERE database = {base:String} ORDER BY name', parameters={'base': DATABASE}).result_rows
    if not filas:
        raise SystemExit(f'La base de datos {DATABASE} está vacía. Ejecuta antes el pipeline: python main.py')
    carpeta.mkdir(parents=True)
    tablas = []
    filas = [f for f in filas if f[0] not in PRIVADAS and (f[1] == 'View' or solo is None or f[0] in solo)]
    for nombre, motor, sql in filas:
        vista = motor == 'View'
        tablas.append({'nombre': nombre, 'vista': vista, 'sql': sql})
        if not vista:
            # bloques pequeños: el contenedor los inserta uno a uno sin pasarse de memoria (cargar_instantanea.py)
            datos = cliente.raw_query(f'SELECT * FROM {DATABASE}.{nombre}', fmt='Native',
                                      settings={'max_block_size': 8192})
            (carpeta / f'{nombre}.native.gz').write_bytes(gzip.compress(datos))
    (carpeta / 'tablas.json').write_text(json.dumps({'base': DATABASE, 'tablas': tablas}, ensure_ascii=False, indent=1),
                                         encoding='utf-8')
    return len(filas)


def publicar_modelos():
    """Las cifras de la web de modelos, calculadas aquí: el contenedor las sirve tal cual, sin pandas ni parquet."""
    from fastapi import HTTPException
    from web_modelos import app as web_modelos
    try:
        datos = web_modelos.modelos()
    except HTTPException as e:
        raise SystemExit(e.detail)
    datos.pop('web_casos', None)
    (SALIDA / 'web_modelos' / 'publicado.json').write_text(json.dumps(datos, ensure_ascii=False), encoding='utf-8')


def vaciar(carpeta):
    if carpeta.exists():
        shutil.rmtree(carpeta, onerror=lambda f, ruta, _: (Path(ruta).chmod(0o700), f(ruta)))   # .git es de solo lectura


def preparar():
    vaciar(SALIDA)
    shutil.copytree(PLANTILLA, SALIDA, ignore=shutil.ignore_patterns('__pycache__'))
    for relativa in CODIGO:
        copiar(relativa)
    publicar_modelos()
    n = exportar_base(SALIDA / 'instantanea', TABLAS)
    megas = sum(f.stat().st_size for f in SALIDA.rglob('*') if f.is_file()) / 1e6
    print(f'Contenedor preparado en {SALIDA.relative_to(RAIZ)}: {n} tablas y vistas de {DATABASE}, {megas:.1f} MB')


def subir_git(carpeta, url, rama, mensaje):
    """Sube `carpeta` a la rama `rama` de `url` como una sola versión, sin historia: cada subida reemplaza a la
    anterior (git push --force), así el repositorio no crece con cada día."""
    def git(*args):
        subprocess.run(['git', '-C', str(carpeta), *args], check=True)
    git('init', '-q', '-b', rama)
    git('config', 'core.autocrlf', 'false')   # tal cual: sin convertir saltos de línea en Windows
    git('add', '-A')
    git('commit', '-q', '-m', mensaje)
    git('push', '-q', '--force', url, rama)


def publicar_git(url):
    subir_git(SALIDA, url, 'main', 'Centinela: código, resultados de los modelos y copia de la base de datos')
    print('Subido. Si es la primera vez, en https://dashboard.render.com: New > Blueprint y elegir ese repositorio.\n'
          'Después Render vuelve a construir solo con cada publicación (pestaña "Events" del servicio).')


def subir_huggingface(space, privado):
    from huggingface_hub import HfApi   # solo hace falta para subir: pip install huggingface_hub
    api = HfApi()
    api.create_repo(space, repo_type='space', space_sdk='docker', private=privado, exist_ok=True)
    api.upload_folder(repo_id=space, repo_type='space', folder_path=SALIDA,
                      commit_message='Centinela: código, resultados de los modelos y copia de la base de datos')
    web = 'https://' + space.replace('/', '-').replace('_', '-').replace('.', '-').lower() + '.hf.space'
    print(f'Subido. Hugging Face construye la imagen en unos minutos (pestaña "Logs" del Space).\n'
          f'  Space: https://huggingface.co/spaces/{space}\n'
          f'  Web:   {web}')


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0].strip())
    destino = p.add_mutually_exclusive_group()
    destino.add_argument('--git', metavar='URL', help='repositorio de GitHub que construye Render')
    destino.add_argument('--huggingface', metavar='USUARIO/SPACE', help='Space de Hugging Face (Docker; plan PRO)')
    p.add_argument('--privado', action='store_true', help='con --huggingface: Space privado')
    args = p.parse_args()
    preparar()
    if args.git:
        publicar_git(args.git)
    elif args.huggingface:
        subir_huggingface(args.huggingface, args.privado)


if __name__ == '__main__':
    main()
