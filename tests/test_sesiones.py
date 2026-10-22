"""Inicio de sesión (web/sesiones.py): contraseñas, cookie, cerrar sesión y lo que el servidor impone a cada
rol. Con un almacén en memoria y una app mínima: no necesita ClickHouse."""
from datetime import datetime

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from web import sesiones


class AlmacenSimulado:
    """Lo mismo que sesiones.Almacen, en memoria."""
    def __init__(self):
        self.usuarios, self.sesiones = {}, {}

    def guardar_usuario(self, email, nombre, rol, comercio_id, contrasena, activo=True, provisional=False):
        sal, hash_ = sesiones.cifrar(contrasena)
        estado = "activa" if activo else "desactivada"
        self.usuarios[email] = {"email": email, "nombre": nombre, "rol": rol, "comercio_id": comercio_id,
                                "sal": sal, "hash": hash_, "estado": estado, "activo": estado == "activa",
                                "cambiar_contrasena": int(provisional)}

    def ficha(self, email):
        return self.usuarios.get(email.strip().lower())

    def usuario(self, email):
        u = self.ficha(email)
        return u if u and u["activo"] else None

    def abrir_sesion(self, email, duracion):
        token = f"t{len(self.sesiones)}"
        self.sesiones[token] = (email, datetime.now() + duracion)
        return token

    def cerrar_sesion(self, token):
        self.sesiones.pop(token, None)

    def de_token(self, token):
        email, caduca = self.sesiones.get(token, (None, None))
        u = self.usuario(email) if email and caduca > datetime.now() else None
        return {k: u[k] for k in ("email", "nombre", "rol", "comercio_id", "cambiar_contrasena")} if u else None


def app_de_prueba(almacen, **opciones):
    """Una web mínima protegida que devuelve lo que le llega (para ver qué cambia el middleware)."""
    app = FastAPI()

    @app.get("/")
    def portada():
        return {"ok": True}

    @app.get("/api/eco")
    def eco(request: Request):
        return dict(request.query_params)

    @app.post("/api/eco")
    async def eco_post(request: Request):
        return await request.json()

    app.add_middleware(sesiones.ProtegerWeb, almacen=lambda: almacen, publicas={"/login", "/api/login"}, **opciones)
    app.include_router(sesiones.crear_router(lambda: almacen))
    return app


@pytest.fixture
def almacen(monkeypatch):
    monkeypatch.setattr(sesiones.time, "sleep", lambda s: None)     # sin la espera tras una contraseña mala
    a = AlmacenSimulado()
    a.guardar_usuario("jefa@banco.com", "Jefa", "administrador", 0, "secreta-0")
    a.guardar_usuario("ana@banco.com", "Ana", "analista", 0, "secreta-1")
    a.guardar_usuario("tienda@moda.com", "Moda Sol", "comercio", 12, "secreta-2")
    return a


def entrar(cliente, email, contrasena, recordar=False):
    return cliente.post("/api/login", json={"email": email, "contrasena": contrasena, "recordar": recordar})


def test_contrasenas_cifradas():
    sal, hash_ = sesiones.cifrar("hola")
    assert hash_ != "hola" and sesiones.comprobar("hola", sal, hash_) and not sesiones.comprobar("adios", sal, hash_)
    assert sesiones.cifrar("hola")[1] != hash_                      # cada cuenta con su sal


def test_sin_sesion(almacen):
    c = TestClient(app_de_prueba(almacen))
    assert c.get("/api/eco").status_code == 401
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?volver=")


def test_entrar_y_salir(almacen):
    c = TestClient(app_de_prueba(almacen))
    assert entrar(c, "ana@banco.com", "mala").status_code == 401
    assert entrar(c, "nadie@banco.com", "secreta-1").status_code == 401
    r = entrar(c, " ANA@banco.com ", "secreta-1")
    assert r.status_code == 200 and r.json()["rol"] == "analista"
    galleta = r.headers["set-cookie"].lower()
    assert "httponly" in galleta and "samesite=lax" in galleta and "max-age" not in galleta   # se va al cerrar el navegador
    assert c.get("/api/sesion").json()["nombre"] == "Ana"
    c.post("/api/logout")
    assert c.get("/api/eco").status_code == 401
    assert almacen.sesiones == {}                                    # la sesión ya no vale aunque se copie la cookie


def test_mantener_la_sesion(almacen):
    r = entrar(TestClient(app_de_prueba(almacen)), "ana@banco.com", "secreta-1", recordar=True)
    assert f"max-age={30 * 24 * 3600}" in r.headers["set-cookie"].lower()


def test_cuenta_desactivada_no_entra(almacen):
    """Solo se dice que está desactivada si la contraseña es la buena (si no, cualquiera sabría quién tiene cuenta)."""
    almacen.guardar_usuario("antigua@tienda.com", "Antigua", "comercio", 5, "secreta-3", activo=False)
    c = TestClient(app_de_prueba(almacen))
    r = entrar(c, "antigua@tienda.com", "secreta-3")
    assert r.status_code == 403 and "desactivada" in r.json()["detail"]
    assert entrar(c, "antigua@tienda.com", "mala").status_code == 401
    assert c.get("/api/eco").status_code == 401


def test_rutas_del_administrador(almacen):
    """La gestión de cuentas: ni un comercio ni un analista, solo el administrador."""
    c = TestClient(app_de_prueba(almacen, rutas_admin=("/api/eco",)))
    for email, clave in [("tienda@moda.com", "secreta-2"), ("ana@banco.com", "secreta-1")]:
        entrar(c, email, clave)
        assert c.get("/api/eco").status_code == 403, email
    entrar(c, "jefa@banco.com", "secreta-0")
    assert c.get("/api/eco").status_code == 200


def test_cuenta_desactivada(almacen):
    c = TestClient(app_de_prueba(almacen))
    entrar(c, "ana@banco.com", "secreta-1")
    almacen.usuarios["ana@banco.com"]["activo"] = False
    assert c.get("/api/eco").status_code == 401


def test_un_analista_elige_comercio(almacen):
    c = TestClient(app_de_prueba(almacen))
    entrar(c, "ana@banco.com", "secreta-1")
    assert c.get("/api/eco", params={"comercio": 7, "demo": "true"}).json() == {"comercio": "7", "demo": "true"}
    assert c.post("/api/eco", json={"usuario": "otro", "comercio": 7}).json() == {"usuario": "Ana", "comercio": 7}


def test_un_comercio_solo_ve_lo_suyo(almacen):
    c = TestClient(app_de_prueba(almacen))
    entrar(c, "tienda@moda.com", "secreta-2")
    # Pida lo que pida la página: su comercio y sin la verdad del simulador
    assert c.get("/api/eco", params={"comercio": 0, "demo": "true", "dias": 7}).json() == {"dias": "7", "comercio": "12"}
    assert c.get("/api/eco").json() == {"comercio": "12"}
    assert c.post("/api/eco", json={"usuario": "Ana", "comercio": 0, "veredicto": "fraude"}).json() == \
        {"usuario": "Moda Sol", "comercio": 12, "veredicto": "fraude"}


def test_solo_el_administrador(almacen):
    """La web de modelos: solo el administrador; sin sesión, a iniciar sesión en la web de casos."""
    c = TestClient(app_de_prueba(almacen, solo_roles=("administrador",), login=lambda r: "http://casos:8000/login"))
    r = c.get("/", follow_redirects=False)
    assert r.headers["location"].startswith("http://casos:8000/login?volver=http")
    for email, clave in [("tienda@moda.com", "secreta-2"), ("ana@banco.com", "secreta-1")]:
        entrar(c, email, clave)
        assert c.get("/api/eco").status_code == 403, email
    assert c.get("/", follow_redirects=False).headers["location"] == "http://casos:8000/"
    entrar(c, "jefa@banco.com", "secreta-0")
    assert c.get("/api/eco").status_code == 200
