"""Conectar con Microsoft Teams (web/teams.py y web/microsoft.py) y publicar los avisos en el canal elegido
(pipeline/notificar.py). Con un Microsoft de mentira: no se conecta con el de verdad ni hace falta ClickHouse."""
import sys
import urllib.parse
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import notificar
from web import microsoft

CONTRASENA = "prueba-de-tests-123"
EQUIPOS = [{"id": "eq1", "displayName": "Centinela avisos"}]
CANALES = {"eq1": [{"id": "19:canal1@thread.tacv2", "displayName": "General"}]}


class MicrosoftDeMentira:
    """Responde como el inicio de sesión de Microsoft y Graph. Cada renovación da un refresh token nuevo."""
    def __init__(self):
        self.publicados, self.renovaciones = [], 0

    def __call__(self, url, datos=None, token=None, json_cuerpo=None):
        if url.endswith("/token"):
            if datos["grant_type"] == "authorization_code":
                assert datos["code"] == "codigo-bueno" and datos["redirect_uri"].endswith("/api/teams/vuelta")
                return {"access_token": "acceso-1", "refresh_token": "permiso-1"}
            self.renovaciones += 1
            return {"access_token": f"acceso-r{self.renovaciones}", "refresh_token": f"permiso-r{self.renovaciones}"}
        if "/me?" in url:
            return {"mail": "clara@empresa.es"}
        if url.endswith("joinedTeams?$select=id,displayName"):
            return {"value": EQUIPOS}
        if "/channels?" in url:
            return {"value": CANALES[url.split("/teams/")[1].split("/")[0]]}
        if url.endswith("/messages"):
            self.publicados.append((url, json_cuerpo["body"]["content"]))
            return {"id": "mensaje"}
        raise AssertionError(f"no esperada: {url}")


@pytest.fixture
def entorno(monkeypatch):
    from web.app import _almacen, app
    monkeypatch.setenv("MS_CLIENTE_ID", "cliente-de-prueba")
    monkeypatch.setenv("MS_SECRETO", "secreto-de-prueba")
    falso = MicrosoftDeMentira()
    monkeypatch.setattr(microsoft, "_peticion", falso)
    email = "teams@tests.centinela"
    _almacen.guardar_usuario(email, "Teams", "analista", 0, CONTRASENA)
    web = TestClient(app)
    web.post("/api/login", json={"email": email, "contrasena": CONTRASENA})
    yield {"web": web, "almacen": _almacen, "email": email, "microsoft": falso, "app": app}
    _almacen.borrar_usuarios("@tests.centinela")


def conectar(web):
    r = web.get("/api/teams/conectar", follow_redirects=False)
    destino = urllib.parse.urlparse(r.headers["location"])
    parametros = dict(urllib.parse.parse_qsl(destino.query))
    return r, destino, parametros


def test_conectar_lleva_a_microsoft(entorno):
    assert entorno["web"].get("/api/teams").json()["disponible"] is True
    r, destino, p = conectar(entorno["web"])
    assert r.status_code == 303 and destino.netloc == "login.microsoftonline.com"
    assert p["client_id"] == "cliente-de-prueba" and p["redirect_uri"] == "http://testserver/api/teams/vuelta"
    assert "ChannelMessage.Send" in p["scope"] and p["state"]


def test_vuelta_sin_el_estado_bueno(entorno):
    """Otra web no puede colarnos su propio inicio de sesión: el estado tiene que ser el que pusimos."""
    conectar(entorno["web"])
    r = entorno["web"].get("/api/teams/vuelta", params={"code": "codigo-bueno", "state": "otro"}, follow_redirects=False)
    assert "teams=error" in r.headers["location"]
    assert entorno["web"].get("/api/teams").json()["conectado"] is False


def test_microsoft_no_da_el_permiso(entorno):
    _, _, p = conectar(entorno["web"])
    r = entorno["web"].get("/api/teams/vuelta", follow_redirects=False,
                           params={"error": "consent_required", "error_description": "Need admin approval",
                                   "state": p["state"]})
    assert "teams=error" in r.headers["location"] and "admin" in urllib.parse.unquote_plus(r.headers["location"])


def test_conectar_elegir_canal_publicar_y_desconectar(entorno, monkeypatch):
    web, almacen, email = entorno["web"], entorno["almacen"], entorno["email"]
    _, _, p = conectar(web)
    r = web.get("/api/teams/vuelta", params={"code": "codigo-bueno", "state": p["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/?teams=elegir"
    guardado = almacen.ajustes(email)["teams_refresh"]
    assert "permiso-1" not in guardado and microsoft.descifrar(guardado) == "permiso-1"   # se guarda cifrado
    assert web.get("/api/teams").json() == {"disponible": True, "conectado": True, "cuenta": "clara@empresa.es",
                                            "equipo": "", "canal": ""}
    (canal,) = web.get("/api/teams/canales").json()["canales"]
    assert canal["equipo"] == "Centinela avisos" and canal["canal"] == "General"
    assert web.post("/api/teams/canal", json={**canal, "canal_id": "19:otro"}).status_code == 422   # no es suyo
    assert web.post("/api/teams/canal", json=canal).json()["canal"] == "General"

    # El aviso de la noche: notificar.py lo publica en ese canal (y guarda el permiso renovado)
    for v in ["TEAMS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_SERVIDOR", "BREVO_API_KEY"]:
        monkeypatch.setenv(v, "")
    aviso = {"dia": "2026-10-23", "nivel": "critico", "comercios": [1], "mensaje": "Tarjeta 1234: <2 pagos> raros"}
    assert notificar.notificar_avisos([aviso]) == 1
    ((url, contenido),) = entorno["microsoft"].publicados
    assert "/teams/eq1/channels/" in url and "&lt;2 pagos&gt;" in contenido          # sin HTML colado
    assert microsoft.descifrar(almacen.ajustes(email)["teams_refresh"]).startswith("permiso-r")

    web.post("/api/teams/desconectar", json={})
    assert web.get("/api/teams").json()["conectado"] is False
    assert "teams_graph" not in notificar.canales_de({"email": email, **almacen.ajustes(email)})


def test_sin_configurar_y_como_invitado(entorno, monkeypatch):
    invitado = TestClient(entorno["app"])
    invitado.post("/api/invitado", json={"rol": "analista"})
    assert invitado.get("/api/teams/conectar", follow_redirects=False).status_code == 403
    monkeypatch.setenv("MS_CLIENTE_ID", "")
    assert entorno["web"].get("/api/teams").json()["disponible"] is False
    assert entorno["web"].get("/api/teams/conectar", follow_redirects=False).status_code == 503
