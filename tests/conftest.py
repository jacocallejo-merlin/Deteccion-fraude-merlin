import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Las cuentas de los tests, en un SQLite temporal: nunca en la base de datos de cuentas de verdad (CUENTAS_URL del
# .env, compartida por todos). Va antes de importar nada: load_dotenv no pisa lo que ya está puesto.
os.environ['CUENTAS_URL'] = f'sqlite:///{Path(tempfile.mkdtemp(prefix="centinela_tests_")) / "cuentas.sqlite"}'


@pytest.fixture(scope="session")
def clickhouse():
    """Cliente de ClickHouse con el pipeline ya ejecutado. Si no está disponible, el test se salta."""
    import clickhouse_connect
    from config import HOST, PORT, USER, PASSWORD, DATABASE
    try:
        client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER,
                                               password=PASSWORD, database=DATABASE)
        if not client.command("EXISTS TABLE features_transaccion"):
            pytest.skip("features_transaccion no existe: ejecuta antes main.py")
    except Exception as e:
        pytest.skip(f"ClickHouse no disponible: {e}")
    return client
