"""Pagos de los comercios que se registran en la web (generar_lote.py, comercios_registrados): desde el día
siguiente a su alta, de los clientes simulados, por sus propios canales y con algún caso de fraude. Sin ClickHouse:
solo la generación en Python."""
import copy
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))   # generar_lote importa dataset
from pipeline import generar_lote as gl

SEMILLA = 42
INICIO = datetime(2026, 10, 24)
CONTADORES = {"sesion": 1_000, "evento": 5_000, "devolucion": 100, "transaccion": 2_000}


def comercio(comercio_id=100_001, alta=date(2026, 10, 20), pagos_dia=40):
    return {"comercio_id": comercio_id, "nombre": "Tienda Tests", "categoria": "moda", "pais": "España",
            "ciudad": "Vigo", "alta": alta, "tamano": "mediano", "pagos_dia": pagos_dia}


@pytest.fixture(scope="module")
def mundo():
    return gl.generar_mundo()


def generar(mundo, registrados, inicio=INICIO):
    """Un día de pagos de esos comercios, con el mundo y los dispositivos libres recién copiados (como cada lote)."""
    m = copy.deepcopy(mundo)
    return gl.pagos_de_registrados(m, inicio, inicio + timedelta(days=1), SEMILLA, registrados,
                                   list(m.dispositivos_sin_dueno), CONTADORES)


def test_mismo_dia_y_mismo_comercio_dan_lo_mismo(mundo):
    a, casos_a = generar(mundo, [comercio()])
    b, casos_b = generar(mundo, [comercio()])
    assert a.tablas == b.tablas and casos_a == casos_b
    assert len(a.tablas["transaccion"]) > 10


def test_sus_pagos_van_por_sus_canales_y_siguen_la_numeracion(mundo):
    fab, _ = generar(mundo, [comercio()])
    canales = {c["canal_id"] for c in gl.canales_de(100_001, "Vigo")}
    assert {t["canal_id"] for t in fab.tablas["transaccion"]} <= canales
    assert all(c // 10 == 100_001 for c in canales) and any(
        c["tipo"] != "datafono" for c in gl.canales_de(100_001, "Vigo"))          # siempre hay un canal online
    assert min(t["transaccion_id"] for t in fab.tablas["transaccion"]) > CONTADORES["transaccion"]
    assert min(s["sesion_id"] for s in fab.tablas["sesion"]) > CONTADORES["sesion"]
    assert all(INICIO <= t["timestamp"] < INICIO + timedelta(days=2) for t in fab.tablas["transaccion"])


def test_empieza_el_dia_siguiente_al_alta(mundo):
    assert not generar(mundo, [comercio(alta=INICIO.date())])[0].tablas["transaccion"]      # alta hoy: aún nada
    assert generar(mundo, [comercio(alta=INICIO.date() - timedelta(days=1))])[0].tablas["transaccion"]


def test_un_comercio_nuevo_no_cambia_los_pagos_de_los_anteriores(mundo):
    solo, _ = generar(mundo, [comercio()])
    con_otro, _ = generar(mundo, [comercio(), comercio(100_002, pagos_dia=15)])
    n = len(solo.tablas["transaccion"])
    assert con_otro.tablas["transaccion"][:n] == solo.tablas["transaccion"]
    assert {t["canal_id"] // 10 for t in con_otro.tablas["transaccion"][n:]} == {100_002}


def test_el_tamano_marca_cuantos_pagos_le_llegan(mundo):
    pequeno = sum(len(generar(mundo, [comercio(pagos_dia=15)], INICIO + timedelta(days=d))[0].tablas["transaccion"])
                  for d in range(10))
    grande = sum(len(generar(mundo, [comercio(pagos_dia=80)], INICIO + timedelta(days=d))[0].tablas["transaccion"])
                 for d in range(10))
    assert 100 < pequeno < 300 and grande > 3 * pequeno     # ~15 y ~80 al día (más compras extra de la misma sesión)


def test_recibe_algun_caso_de_fraude(mundo):
    semana = gl.REGISTRADOS.get("casos_fraude_semana", 2)
    casos = sum(generar(mundo, [comercio(pagos_dia=15)], INICIO + timedelta(days=d))[1] for d in range(28))
    assert 0.4 * 4 * semana <= casos <= 2 * 4 * semana       # unas 4 semanas
