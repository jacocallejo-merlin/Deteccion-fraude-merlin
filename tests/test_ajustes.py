"""Mis avisos (web/ajustes.py): cada usuario elige su correo, su Teams y qué avisar, y pipeline/notificar.py
lo usa. Con una base de datos de cuentas nueva (SQLite) en cada test: no envía nada real."""
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import notificar
from web.ajustes import crear_router
from web.bd_cuentas import BaseCuentas
from web.sesiones import Almacen

USUARIOS = {"analista": {"email": "ana@banco.com", "nombre": "Ana", "rol": "analista", "comercio_id": 0},
            "comercio": {"email": "tienda@moda.com", "nombre": "Moda Sol", "rol": "comercio", "comercio_id": 12}}


@pytest.fixture
def web(monkeypatch, tmp_path):
    for v in ["TEAMS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_SERVIDOR", "CORREO_DESTINATARIOS"]:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setitem(notificar._cfg, "correo", {})
    enviados = []
    monkeypatch.setattr(notificar, "_post_json", lambda url, cuerpo: enviados.append(url) or 200)
    almacen = Almacen(BaseCuentas(f"sqlite:///{tmp_path / 'cuentas.sqlite'}"))   # una base de datos nueva, vacía
    quien = {"rol": "analista"}

    app = FastAPI()

    @app.middleware("http")
    async def sesion(request, siguiente):          # lo que haría web/sesiones.py
        request.scope["usuario"] = USUARIOS[quien["rol"]]
        return await siguiente(request)
    app.include_router(crear_router(lambda: almacen))
    return TestClient(app), almacen, enviados, quien


def test_por_defecto_a_su_correo(web, monkeypatch):
    cliente, _, _, _ = web
    a = cliente.get("/api/ajustes").json()
    assert a["email"] == "ana@banco.com" and a["correo"] and a["avisos_fraude"] and a["sistema"]
    assert a["correo_servidor"] is False                        # sin servidor de correo, se avisa en pantalla
    monkeypatch.setenv("SMTP_SERVIDOR", "smtp.ejemplo")
    assert cliente.get("/api/ajustes").json()["correo_servidor"] is True


def test_guardar_y_usar_teams(web):
    cliente, almacen, enviados, _ = web
    assert cliente.get("/api/ajustes").json()["teams"]["activo"] is False
    r = cliente.post("/api/ajustes", json={"teams_webhook_url": " https://teams.ejemplo/flujo-de-ana-123456 "})
    assert r.status_code == 200
    assert r.json()["teams"] == {"activo": True, "final": "…123456"}   # la dirección completa no se devuelve nunca
    assert almacen.ajustes("ana@banco.com")["teams_webhook_url"] == "https://teams.ejemplo/flujo-de-ana-123456"
    # El mensaje de prueba va solo a los canales de esta persona
    assert cliente.post("/api/ajustes/prueba", json={}).json()["enviados"] == 1
    assert enviados == ["https://teams.ejemplo/flujo-de-ana-123456"]


def test_cada_uno_lo_suyo(web):
    cliente, almacen, _, quien = web
    cliente.post("/api/ajustes", json={"correo": False, "avisos_fraude": False})
    quien["rol"] = "comercio"
    a = cliente.get("/api/ajustes").json()
    assert a["email"] == "tienda@moda.com" and a["correo"] and a["avisos_fraude"]   # lo de Ana no le afecta
    assert a["sistema"] is None                                  # los avisos del sistema no son para comercios
    cliente.post("/api/ajustes", json={"sistema": True})
    assert "sistema" not in almacen.ajustes("tienda@moda.com")
    assert almacen.ajustes("ana@banco.com")["correo"] == "0"


def test_entradas_no_validas(web):
    cliente, almacen, _, _ = web
    assert cliente.post("/api/ajustes", json={"teams_webhook_url": "http://inseguro"}).status_code == 422
    assert almacen.ajustes("ana@banco.com") == {}                # nada guardado
    r = cliente.post("/api/ajustes/prueba", json={}).json()
    assert r["enviados"] == 0 and "ningún canal" in r["detalle"][0]
