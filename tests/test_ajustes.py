"""Mis avisos (web/ajustes.py): cada usuario elige su correo, su Teams y qué avisar, y pipeline/notificar.py
lo usa. Con una base de datos simulada: no toca ClickHouse ni envía nada real."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import notificar
from web.ajustes import crear_router

USUARIOS = {"analista": {"email": "ana@banco.com", "nombre": "Ana", "rol": "analista", "comercio_id": 0},
            "comercio": {"email": "tienda@moda.com", "nombre": "Moda Sol", "rol": "comercio", "comercio_id": 12}}


class BaseSimulada:
    """Lo mínimo de ClickHouse que usa web/ajustes.py: la tabla ajustes_usuario."""
    def __init__(self):
        self.tabla = {}

    def command(self, sql):
        pass

    def query(self, sql, parameters):
        filas = [(clave, valor) for (email, clave), valor in self.tabla.items() if email == parameters["e"]]
        return type("Resultado", (), {"result_rows": filas})()

    def insert(self, tabla, filas, column_names):
        self.tabla.update({(email, clave): valor for email, clave, valor in filas})


@pytest.fixture
def web(monkeypatch):
    for v in ["TEAMS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_SERVIDOR", "CORREO_DESTINATARIOS"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setitem(notificar._cfg, "correo", {})
    enviados = []
    monkeypatch.setattr(notificar, "_post_json", lambda url, cuerpo: enviados.append(url) or 200)
    base = BaseSimulada()
    quien = {"rol": "analista"}

    app = FastAPI()

    @app.middleware("http")
    async def sesion(request, siguiente):          # lo que haría web/sesiones.py
        request.scope["usuario"] = USUARIOS[quien["rol"]]
        return await siguiente(request)
    app.include_router(crear_router(lambda: base))
    return TestClient(app), base, enviados, quien


def test_por_defecto_a_su_correo(web, monkeypatch):
    cliente, _, _, _ = web
    a = cliente.get("/api/ajustes").json()
    assert a["email"] == "ana@banco.com" and a["correo"] and a["avisos_fraude"] and a["sistema"]
    assert a["correo_servidor"] is False                        # sin servidor de correo, se avisa en pantalla
    monkeypatch.setenv("SMTP_SERVIDOR", "smtp.ejemplo")
    assert cliente.get("/api/ajustes").json()["correo_servidor"] is True


def test_guardar_y_usar_teams(web):
    cliente, base, enviados, _ = web
    assert cliente.get("/api/ajustes").json()["teams"]["activo"] is False
    r = cliente.post("/api/ajustes", json={"teams_webhook_url": " https://teams.ejemplo/flujo-de-ana-123456 "})
    assert r.status_code == 200
    assert r.json()["teams"] == {"activo": True, "final": "…123456"}   # la dirección completa no se devuelve nunca
    assert base.tabla[("ana@banco.com", "teams_webhook_url")] == "https://teams.ejemplo/flujo-de-ana-123456"
    # El mensaje de prueba va solo a los canales de esta persona
    assert cliente.post("/api/ajustes/prueba", json={}).json()["enviados"] == 1
    assert enviados == ["https://teams.ejemplo/flujo-de-ana-123456"]


def test_cada_uno_lo_suyo(web):
    cliente, base, _, quien = web
    cliente.post("/api/ajustes", json={"correo": False, "avisos_fraude": False})
    quien["rol"] = "comercio"
    a = cliente.get("/api/ajustes").json()
    assert a["email"] == "tienda@moda.com" and a["correo"] and a["avisos_fraude"]   # lo de Ana no le afecta
    assert a["sistema"] is None                                  # los avisos del sistema no son para comercios
    cliente.post("/api/ajustes", json={"sistema": True})
    assert ("tienda@moda.com", "sistema") not in base.tabla
    assert base.tabla[("ana@banco.com", "correo")] == "0"


def test_entradas_no_validas(web):
    cliente, base, _, _ = web
    assert cliente.post("/api/ajustes", json={"teams_webhook_url": "http://inseguro"}).status_code == 422
    assert base.tabla == {}                                      # nada guardado
    r = cliente.post("/api/ajustes/prueba", json={}).json()
    assert r["enviados"] == 0 and "ningún canal" in r["detalle"][0]
