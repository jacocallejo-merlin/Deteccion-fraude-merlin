"""Todo el código se puede leer: sin errores de sintaxis ni marcas de un choque de Git sin resolver
(<<<<<<< ... >>>>>>>), que se cuelan al hacer git stash pop o git pull con cambios a medias. Un archivo así
no falla al subirlo: falla después, por ejemplo en el scoring de la noche. No necesita ClickHouse."""
import py_compile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
CARPETAS = ['pipeline', 'web', 'web_modelos', 'despliegue', 'dags', 'tests', 'k8s', '.github']
EXTENSIONES = {'.py', '.js', '.html', '.css', '.yaml', '.yml', '.md', '.txt', '.sh'}
MARCAS = ('<<<<<<< ', '>>>>>>> ')


def archivos():
    sueltos = [p for p in RAIZ.iterdir() if p.is_file() and p.suffix in EXTENSIONES]
    en_carpetas = [p for c in CARPETAS if (RAIZ / c).exists() for p in (RAIZ / c).rglob('*')
                   if p.is_file() and p.suffix in EXTENSIONES and '_contenedor' not in p.parts
                   and '_estado' not in p.parts and '__pycache__' not in p.parts]
    return sueltos + en_carpetas


@pytest.mark.parametrize('ruta', [p for p in archivos() if p.suffix == '.py'], ids=lambda p: str(p.relative_to(RAIZ)))
def test_compila(ruta):
    py_compile.compile(str(ruta), doraise=True)


def test_sin_choques_de_git():
    con_marcas = [str(p.relative_to(RAIZ)) for p in archivos()
                  if any(l.startswith(MARCAS) for l in p.read_text(encoding='utf-8', errors='ignore').splitlines())]
    assert not con_marcas, f'Choque de Git sin resolver en: {", ".join(con_marcas)}'
