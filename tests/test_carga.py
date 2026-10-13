"""La conversión de fechas de carga_transacciones.py, probada en ClickHouse con casos conocidos."""
import pytest

from pipeline.carga_transacciones import FECHA

CASOS = [
    ("2026-03-05 11:22:33", "2026-03-05 11:22:33"),          # formato habitual (UTC)
    ("05/03/2026 11:22:33", "2026-03-05 11:22:33"),          # día primero (UTC)
    ("2026-03-05T12:22:33+01:00", "2026-03-05 11:22:33"),    # Madrid en invierno
    ("2026-07-05T13:22:33+02:00", "2026-07-05 11:22:33"),    # Madrid en verano
    ("31/02/2026 10:15:00", None),                           # fecha imposible
    ("2026-13-01 25:00:00", None),
    ("0000-00-00 00:00:00", None),
    ("", None),
    ("sin fecha", None),
]


@pytest.mark.parametrize("texto, esperado", CASOS)
def test_conversion_de_fechas(clickhouse, texto, esperado):
    sql = f"SELECT toString({FECHA}, 'UTC') FROM (SELECT '{texto}' AS timestamp)"
    assert clickhouse.query(sql).result_rows[0][0] == esperado


def test_ninguna_transaccion_se_pierde(clickhouse):
    brutas = clickhouse.command("SELECT count() FROM transaccion_bruta")
    cargadas = clickhouse.command("SELECT uniqExact(transaccion_id) FROM transaccion")
    cuarentena = clickhouse.command("SELECT uniqExact(transaccion_id) FROM transaccion_cuarentena")
    unicas = clickhouse.command("SELECT uniqExact(transaccion_id) FROM transaccion_bruta")
    assert brutas >= unicas == cargadas + cuarentena


def test_todo_en_euros(clickhouse):
    assert clickhouse.command("SELECT count() FROM transaccion WHERE moneda != 'EUR'") == 0
