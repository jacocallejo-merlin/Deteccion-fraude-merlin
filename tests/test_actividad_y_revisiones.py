"""Registro de actividad (quién hizo qué), avisos leídos de cada persona y revisiones que no se pierden
(web/revisiones.py). Las cuentas van al SQLite temporal de los tests (conftest.py); las revisiones se prueban con
un ClickHouse de mentira, para no escribir revisiones falsas en los datos de verdad."""
import secrets
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

CONTRASENA = "prueba-de-tests-123"


@pytest.fixture
def entorno(monkeypatch):
    from web.app import _almacen, app
    import web.cuentas as cuentas
    monkeypatch.setattr(cuentas.time, "sleep", lambda s: None)
    monkeypatch.setattr("web.sesiones.time.sleep", lambda s: None)
    sufijo = secrets.token_hex(4)
    email = lambda quien: f"{quien}-{sufijo}@tests.centinela"     # noqa: E731
    _almacen.guardar_usuario(email("admin"), "Admin de tests", "administrador", 0, CONTRASENA)
    _almacen.guardar_usuario(email("ana"), "Ana de tests", "analista", 0, CONTRASENA)
    _almacen.guardar_usuario(email("luis"), "Luis de tests", "analista", 0, CONTRASENA)
    clientes = {}
    for quien in ["admin", "ana", "luis"]:
        clientes[quien] = TestClient(app)
        clientes[quien].post("/api/login", json={"email": email(quien), "contrasena": CONTRASENA})
    yield {"app": app, "almacen": _almacen, "email": email, **clientes}
    _almacen.borrar_usuarios("@tests.centinela")


# ---------------------------------------------------------------------------- registro de actividad
def test_queda_apuntado_quien_hizo_que(entorno):
    admin, email = entorno["admin"], entorno["email"]
    TestClient(entorno["app"]).post("/api/login", json={"email": email("ana"), "contrasena": "mala-123!"})
    admin.post("/api/cuentas/crear", json={"rol": "analista", "email": email("nueva"), "nombre": "Nueva"})
    admin.post("/api/cuentas/estado", json={"email": email("luis"), "accion": "desactivar"})
    act = admin.get("/api/cuentas/actividad").json()["actividad"]
    hechos = {(a["email"], a["accion"]) for a in act}
    assert (email("ana"), "entrar_fallido") in hechos and (email("ana"), "entrar") in hechos
    assert (email("admin"), "crear_cuenta") in hechos and (email("admin"), "desactivar") in hechos
    assert next(a for a in act if a["accion"] == "desactivar")["detalle"] == email("luis")
    assert act == sorted(act, key=lambda a: -a["ts"])                       # lo más reciente primero
    # De una sola persona, y solo para el administrador
    solo_ana = admin.get("/api/cuentas/actividad", params={"email": email("ana")}).json()["actividad"]
    assert solo_ana and {a["email"] for a in solo_ana} == {email("ana")}
    assert entorno["ana"].get("/api/cuentas/actividad").status_code == 403


def test_no_apunta_secretos(entorno):
    """La dirección del flujo de Teams es un secreto: en el registro solo sale qué se ha cambiado."""
    entorno["ana"].post("/api/ajustes", json={"teams_webhook_url": "https://teams.ejemplo/secreto-123456"})
    act = entorno["admin"].get("/api/cuentas/actividad", params={"email": entorno["email"]("ana")}).json()["actividad"]
    cambio = next(a for a in act if a["accion"] == "mis_avisos")
    assert "teams_webhook_url" in cambio["detalle"] and "secreto" not in cambio["detalle"]


# ---------------------------------------------------------------------------- avisos leídos de cada persona
def test_cada_analista_tiene_sus_avisos_leidos(entorno, clickhouse):
    if not clickhouse.command("EXISTS TABLE aviso") or not clickhouse.command("SELECT count() FROM aviso"):
        pytest.skip("no hay avisos: ejecuta el pipeline completo")
    ana, luis = entorno["ana"], entorno["luis"]
    aviso = ana.get("/api/avisos", params={"limite": 1}).json()["avisos"][0]
    antes = luis.get("/api/avisos", params={"limite": 500}).json()
    assert ana.post("/api/avisos/leidos", json={"aviso_ids": [aviso["aviso_id"]]}).json()["marcados"] == 1
    de_ana = {a["aviso_id"]: a["leido"] for a in ana.get("/api/avisos", params={"limite": 500}).json()["avisos"]}
    de_luis = luis.get("/api/avisos", params={"limite": 500}).json()
    assert de_ana[aviso["aviso_id"]] is True or de_ana[aviso["aviso_id"]] == 1
    assert de_luis["no_leidos"] == antes["no_leidos"]                         # a Luis no le cambia nada


# ---------------------------------------------------------------------------- revisiones que no se pierden
class ClickHouseDeMentira:
    """Lo que usa web/revisiones.py de ClickHouse: la tabla revision_web y el primer día de los datos."""
    def __init__(self, database="fraude_pagos", primer_dia="2026-03-01"):
        self.database, self.primer_dia, self.revision_web = database, primer_dia, []

    def command(self, sql):
        pass

    def query(self, sql, parameters=None):
        if "min(toDate(timestamp))" in sql:
            filas = [[self.primer_dia]]
        else:
            filas = [[f[0], f[6].replace(tzinfo=None)] for f in self.revision_web]
        return type("Resultado", (), {"result_rows": filas})()

    def insert(self, tabla, filas, column_names):
        assert tabla == "revision_web"
        self.revision_web += [list(f) for f in filas]


def test_las_revisiones_se_recuperan(entorno):
    from web import revisiones
    almacen = entorno["almacen"]
    clickhouse = ClickHouseDeMentira()
    revisiones.guardar(almacen, clickhouse, [[123, "fraude", "no lo reconoce", "Ana", 0,
                                              datetime(2026, 9, 6, 0, 0)]], entorno["email"]("ana"))
    assert len(clickhouse.revision_web) == 1
    # La web publicada arranca con ClickHouse recargado: la revisión vuelve, una sola vez
    recargado = ClickHouseDeMentira()
    assert revisiones.sincronizar(almacen, recargado) == 1
    assert recargado.revision_web[0][:4] == [123, "fraude", "no lo reconoce", "Ana"]
    assert revisiones.sincronizar(almacen, recargado) == 0
    # Otros datos (otro ordenador, otra simulación): no se mezclan
    assert revisiones.sincronizar(almacen, ClickHouseDeMentira(primer_dia="2026-05-01")) == 0
