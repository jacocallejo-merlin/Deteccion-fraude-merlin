"""Pantalla de ajustes de avisos (web/ajustes.py): guarda el canal de Teams, los correos y qué avisar, y
pipeline/notificar.py los usa. Con una base de datos simulada: no toca ClickHouse ni envía nada real."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import notificar
from web.ajustes import crear_router


class BaseSimulada:
    """Lo mínimo de ClickHouse que usa web/ajustes.py: guarda las filas de la tabla ajustes."""
    def __init__(self):
        self.tabla = {}

    def command(self, sql):
        pass

    def insert(self, tabla, filas, column_names):
        self.tabla.update({clave: valor for clave, valor in filas})


@pytest.fixture
def web(monkeypatch):
    for v in ["TEAMS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_SERVIDOR", "CORREO_DESTINATARIOS"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setitem(notificar._cfg, "correo", {})
    base = BaseSimulada()
    monkeypatch.setattr(notificar, "ajustes_web", lambda: dict(base.tabla))
    enviados = []
    monkeypatch.setattr(notificar, "_post_json", lambda url, cuerpo: enviados.append(url) or 200)
    app = FastAPI()
    app.include_router(crear_router(lambda: base))
    return TestClient(app), base, enviados


def test_guardar_y_usar_teams(web):
    cliente, base, enviados = web
    assert cliente.get("/api/ajustes").json()["teams"]["activo"] is False
    r = cliente.post("/api/ajustes", json={"teams_webhook_url": " https://teams.ejemplo/flujo-del-banco-123456 "})
    assert r.status_code == 200
    teams = r.json()["teams"]
    assert teams["activo"] and teams["desde_web"]
    assert teams["final"] == "…123456"                       # la dirección completa no se devuelve nunca
    assert base.tabla["teams_webhook_url"] == "https://teams.ejemplo/flujo-del-banco-123456"
    # El mensaje de prueba y los avisos van a esa dirección
    assert cliente.post("/api/ajustes/prueba", json={}).json()["enviados"] == 1
    assert enviados == ["https://teams.ejemplo/flujo-del-banco-123456"]


def test_vaciar_desactiva_el_canal(web, monkeypatch):
    cliente, base, enviados = web
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.ejemplo/del-servidor")
    assert cliente.get("/api/ajustes").json()["teams"]["activo"]           # el de .env, mientras no se toque
    cliente.post("/api/ajustes", json={"teams_webhook_url": ""})
    # Vacío en la web = desactivado, aunque .env tenga uno (el cliente manda)
    assert cliente.get("/api/ajustes").json()["teams"]["activo"] is False


def test_correos_y_que_avisar(web):
    cliente, base, _ = web
    r = cliente.post("/api/ajustes", json={"correo_destinatarios": "a@banco.com; b@banco.com",
                                           "avisos_fraude": False, "drift": True}).json()
    assert r["correo_destinatarios"] == "a@banco.com, b@banco.com"
    assert r["avisos_fraude"] is False and r["drift"] is True
    assert base.tabla["avisos_fraude"] == "0"
    assert notificar.notificar("t", "x", tipo="avisos_fraude") == 0   # apagado desde la web


def test_entradas_no_validas(web):
    cliente, base, _ = web
    assert cliente.post("/api/ajustes", json={"teams_webhook_url": "http://inseguro"}).status_code == 422
    assert cliente.post("/api/ajustes", json={"correo_destinatarios": "a@banco.com, no-es-un-correo"}).status_code == 422
    assert base.tabla == {}                                               # nada guardado
