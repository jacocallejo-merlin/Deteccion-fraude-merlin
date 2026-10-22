"""Cuentas (web/cuentas.py) con la base de datos de verdad: contraseña olvidada con código, registro de un
comercio con código, Configura tu empresa, las cuentas que da el administrador (con contraseña provisional)
y Mi perfil. Necesita ClickHouse con la tabla comercio (el ETL); si no está, se salta. No envía correos:
los códigos se leen de la respuesta, como en local sin servidor de correo. Lo de prueba se borra al acabar."""
import secrets

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

CONTRASENA = "prueba-de-tests-123"


def borrar_cuentas_de_prueba(clickhouse):
    """Para que no se queden en la pestaña Cuentas (también las de test_web.py), ni sus comercios registrados."""
    if clickhouse.command("EXISTS TABLE usuarios"):
        registrados = [c for (c,) in clickhouse.query(
            "SELECT comercio_id FROM usuarios FINAL WHERE endsWith(email, '@tests.centinela') "
            "AND revisada_por = 'registro'").result_rows]
        if registrados:
            for tabla in ["comercio", "empresa"]:
                clickhouse.command(f"ALTER TABLE {tabla} DELETE WHERE comercio_id IN {{c:Array(UInt32)}} "
                                   "SETTINGS mutations_sync = 1", parameters={"c": registrados})
    for tabla in ["usuarios", "codigos", "ajustes_usuario"]:
        if clickhouse.command(f"EXISTS TABLE {tabla}"):
            clickhouse.command(f"ALTER TABLE {tabla} DELETE WHERE endsWith(email, '@tests.centinela') "
                               "SETTINGS mutations_sync = 1")


@pytest.fixture
def entorno(clickhouse, monkeypatch):
    from web.app import _almacen, app          # primero: pone pipeline/ en el path (web.cuentas usa notificar)
    import web.cuentas as cuentas
    monkeypatch.setattr(cuentas.notificar, "a_correo", lambda *a, **k: False)    # sin servidor de correo
    monkeypatch.setattr(cuentas.time, "sleep", lambda s: None)
    sufijo = secrets.token_hex(4)                      # correos nuevos en cada ejecución
    email = lambda quien: f"{quien}-{sufijo}@tests.centinela"     # noqa: E731
    _almacen.guardar_usuario(email("admin"), "Admin de tests", "administrador", 0, CONTRASENA)
    _almacen.guardar_usuario(email("analista"), "Analista de tests", "analista", 0, CONTRASENA)
    admin, analista = TestClient(app), TestClient(app)
    admin.post("/api/login", json={"email": email("admin"), "contrasena": CONTRASENA})
    analista.post("/api/login", json={"email": email("analista"), "contrasena": CONTRASENA})
    comercio = clickhouse.query("SELECT min(comercio_id) FROM comercio").result_rows[0][0]
    yield {"app": app, "almacen": _almacen, "admin": admin, "analista": analista, "email": email,
           "comercio": comercio, "clickhouse": clickhouse}
    borrar_cuentas_de_prueba(clickhouse)


def entrar(cliente, email, contrasena=CONTRASENA):
    return cliente.post("/api/login", json={"email": email, "contrasena": contrasena})


# ---------------------------------------------------------------------------- contraseña olvidada
def test_contrasena_olvidada_con_codigo(entorno):
    email = entorno["email"]("analista")
    anonimo = TestClient(entorno["app"])
    # Misma respuesta exista o no la cuenta (así no se averigua quién tiene cuenta)
    assert anonimo.post("/api/recuperar", json={"email": "nadie@tests.centinela"}).json() == {"ok": True}
    codigo = anonimo.post("/api/recuperar", json={"email": email}).json()["codigo_demo"]
    assert len(codigo) == 6 and codigo.isdigit()
    cambio = {"email": email, "codigo": "000000" if codigo != "000000" else "111111", "contrasena": "nueva-contrasena-1"}
    r = anonimo.post("/api/recuperar/confirmar", json=cambio)
    assert r.status_code == 422 and "te quedan 4" in r.json()["detail"]
    assert anonimo.post("/api/recuperar/confirmar", json={**cambio, "codigo": codigo}).status_code == 200
    assert anonimo.get("/api/sesion").json()["email"] == email             # entra directamente
    assert entorno["analista"].get("/api/sesion").status_code == 401       # su otra sesión, cerrada
    assert entrar(TestClient(entorno["app"]), email).status_code == 401    # la antigua ya no vale
    # El código no vale dos veces
    assert anonimo.post("/api/recuperar/confirmar", json={**cambio, "codigo": codigo}).status_code == 422


def test_codigo_con_demasiados_intentos(entorno):
    email = entorno["email"]("analista")
    anonimo = TestClient(entorno["app"])
    codigo = anonimo.post("/api/recuperar", json={"email": email}).json()["codigo_demo"]
    malo = "000000" if codigo != "000000" else "111111"
    for _ in range(5):
        anonimo.post("/api/recuperar/confirmar", json={"email": email, "codigo": malo, "contrasena": CONTRASENA + "x"})
    r = anonimo.post("/api/recuperar/confirmar", json={"email": email, "codigo": codigo, "contrasena": CONTRASENA + "x"})
    assert r.status_code == 422 and "Demasiados intentos" in r.json()["detail"]


# ---------------------------------------------------------------------------- registro de un comercio
def test_registro_de_un_comercio(entorno):
    email = entorno["email"]("tienda")
    nuevo = TestClient(entorno["app"])
    datos = {"empresa": "Tienda de Tests SL", "nombre": "Laura Gil", "email": email, "contrasena": CONTRASENA}
    codigo = nuevo.post("/api/registro", json=datos).json()["codigo_demo"]
    assert entrar(nuevo, email).status_code == 401                          # sin confirmar el correo, no existe
    r = nuevo.post("/api/registro/confirmar", json={"email": email, "codigo": codigo})
    assert r.status_code == 200 and r.json()["rol"] == "comercio"
    s = nuevo.get("/api/sesion").json()
    assert s["comercio_id"] >= 100_000 and s["comercio_nombre"] == "Tienda de Tests SL"   # comercio nuevo, de cero
    # Configura tu empresa: la primera vez, sin configurar y sin pagos
    e = nuevo.get("/api/empresa").json()
    assert e["configurada"] is False and e["registrada"] is True and e["pagos"] == 0 and e["sectores"]
    empresa = {"nombre": "Tienda Tests", "cif": "b-1234567-4", "categoria": "moda", "pais": "España",
               "ciudad": "Vigo", "telefono": "986 000 000"}
    for campo in ["nombre", "cif", "telefono", "ciudad"]:                    # obligatorios
        assert nuevo.post("/api/empresa", json={**empresa, campo: ""}).status_code == 422, campo
    assert nuevo.post("/api/empresa", json={**empresa, "cif": "B12345675"}).status_code == 422   # control mal
    assert nuevo.post("/api/empresa", json={**empresa, "telefono": "1234"}).status_code == 422
    e = nuevo.post("/api/empresa", json=empresa).json()
    assert e["configurada"] is True and e["cif"] == "B12345674"             # limpio: sin guiones y en mayúsculas
    assert nuevo.get("/api/sesion").json()["comercio_nombre"] == "Tienda Tests"   # el nombre, también en comercio
    # Solo ve lo suyo: ni los demás comercios ni la gestión de cuentas
    assert [c["id"] for c in nuevo.get("/api/contexto").json()["comercios"]] == [s["comercio_id"]]
    assert nuevo.get("/api/transacciones", params={"comercio": 0}).json() == []
    assert nuevo.get("/api/cuentas").status_code == 403
    assert entrar(TestClient(entorno["app"]), email).status_code == 200    # y entra con su contraseña


def test_registro_no_valido(entorno):
    anonimo = TestClient(entorno["app"])
    datos = {"empresa": "Otra", "nombre": "Otro", "email": entorno["email"]("otra"), "contrasena": CONTRASENA}
    assert anonimo.post("/api/registro", json={**datos, "contrasena": "corta"}).status_code == 422
    assert anonimo.post("/api/registro", json={**datos, "contrasena": "sinsimbolo123"}).status_code == 422   # sin símbolo
    assert anonimo.post("/api/registro", json={**datos, "email": "no-es-un-correo"}).status_code == 422
    assert anonimo.post("/api/registro", json={**datos, "email": entorno["email"]("analista")}).status_code == 409
    anonimo.post("/api/registro", json=datos)
    assert anonimo.post("/api/registro/confirmar", json={"email": datos["email"], "codigo": "12345"}).status_code == 422


def test_empresa_solo_para_comercios(entorno):
    assert entorno["analista"].get("/api/empresa").status_code == 403


# ---------------------------------------------------------------------------- cuentas del administrador
def test_solo_el_administrador_da_cuentas(entorno):
    cuerpo = {"rol": "analista", "email": entorno["email"]("x"), "nombre": "X X"}
    assert entorno["analista"].post("/api/cuentas/crear", json=cuerpo).status_code == 403
    assert entorno["analista"].get("/api/cuentas").status_code == 403
    assert TestClient(entorno["app"]).get("/api/cuentas").status_code == 401


def test_contrasena_provisional_hay_que_cambiarla(entorno):
    email = entorno["email"]("nuevo")
    r = entorno["admin"].post("/api/cuentas/crear", json={"rol": "analista", "email": email, "nombre": "Nuevo"}).json()
    provisional = r["contrasena_provisional"]
    nuevo = TestClient(entorno["app"])
    assert entrar(nuevo, email, provisional).json()["cambiar_contrasena"] is True
    # Hasta cambiarla no ve nada
    r = nuevo.get("/api/contexto")
    assert r.status_code == 403 and r.json()["cambiar_contrasena"] is True
    assert nuevo.get("/", follow_redirects=False).headers["location"] == "/login?cambiar=1"
    cambio = {"actual": provisional, "nueva": provisional}
    assert nuevo.post("/api/perfil/contrasena", json=cambio).status_code == 422    # tiene que ser otra
    assert nuevo.post("/api/perfil/contrasena", json={**cambio, "nueva": "mi-contrasena-1"}).status_code == 200
    assert nuevo.get("/api/contexto").status_code == 200
    assert entorno["admin"].post("/api/cuentas/crear", json={"rol": "analista", "email": email,
                                                             "nombre": "Otra vez"}).status_code == 409


def test_cuenta_de_un_comercio_que_ya_existe(entorno):
    email = entorno["email"]("antigua")
    r = entorno["admin"].post("/api/cuentas/crear", json={"rol": "comercio", "email": email,
                                                          "comercio_id": entorno["comercio"]}).json()
    nuevo = TestClient(entorno["app"])
    entrar(nuevo, email, r["contrasena_provisional"])
    nuevo.post("/api/perfil/contrasena", json={"actual": r["contrasena_provisional"], "nueva": CONTRASENA})
    assert nuevo.get("/api/sesion").json()["comercio_id"] == entorno["comercio"]
    e = nuevo.get("/api/empresa").json()
    assert e["registrada"] is False and e["nombre"] and e["pagos"] > 0      # ve su historial


def test_alta_no_valida(entorno):
    crear = lambda **c: entorno["admin"].post("/api/cuentas/crear", json=c).status_code   # noqa: E731
    assert crear(rol="comercio", email=entorno["email"]("t"), comercio_id=99_999_999) == 422
    assert crear(rol="analista", email="no-es-un-correo", nombre="X X") == 422
    assert crear(rol="analista", email=entorno["email"]("t"), nombre="X") == 422


def test_desactivar_y_contrasena_nueva(entorno):
    email = entorno["email"]("analista")
    r = entorno["admin"].post("/api/cuentas/estado", json={"email": email, "accion": "desactivar"})
    assert r.json()["estado"] == "desactivada"
    assert entorno["analista"].get("/api/sesion").status_code == 401       # fuera ya
    r = entrar(TestClient(entorno["app"]), email)
    assert r.status_code == 403 and "desactivada" in r.json()["detail"]
    entorno["admin"].post("/api/cuentas/estado", json={"email": email, "accion": "reactivar"})
    r = entorno["admin"].post("/api/cuentas/estado", json={"email": email, "accion": "contrasena_provisional"}).json()
    assert entrar(TestClient(entorno["app"]), email, r["contrasena_provisional"]).json()["cambiar_contrasena"] is True
    assert entorno["admin"].post("/api/cuentas/estado", json={"email": entorno["email"]("admin"),
                                                              "accion": "desactivar"}).status_code == 422


# ---------------------------------------------------------------------------- Mi perfil
def test_mi_perfil(entorno):
    analista = entorno["analista"]
    p = analista.get("/api/perfil").json()
    assert p["email"] == entorno["email"]("analista") and p["rol"] == "analista"
    p = analista.post("/api/perfil", json={"nombre": "  Nombre Nuevo ", "telefono": "981 000 000"}).json()
    assert p["nombre"] == "Nombre Nuevo" and p["telefono"] == "981 000 000"
    assert analista.get("/api/sesion").json()["nombre"] == "Nombre Nuevo"      # las revisiones, con el nombre nuevo
    assert analista.post("/api/perfil", json={"nombre": "x"}).status_code == 422


def test_cambiar_contrasena_cierra_los_demas_equipos(entorno):
    email = entorno["email"]("analista")
    otro_equipo = TestClient(entorno["app"])
    entrar(otro_equipo, email)
    assert entorno["analista"].get("/api/perfil").json()["otras_sesiones"] >= 1
    cambio = {"actual": "mala", "nueva": "otra-contrasena-456"}
    assert entorno["analista"].post("/api/perfil/contrasena", json=cambio).status_code == 422
    cambio["actual"] = CONTRASENA
    r = entorno["analista"].post("/api/perfil/contrasena", json=cambio)
    assert r.status_code == 200 and r.json()["cerradas"] >= 1
    assert otro_equipo.get("/api/sesion").status_code == 401                 # el otro equipo, fuera
    assert entorno["analista"].get("/api/sesion").status_code == 200        # este sigue dentro


def test_cerrar_la_sesion_en_otros_equipos(entorno):
    otro_equipo = TestClient(entorno["app"])
    entrar(otro_equipo, entorno["email"]("analista"))
    assert entorno["analista"].post("/api/perfil/cerrar-otras", json={}).json()["cerradas"] >= 1
    assert otro_equipo.get("/api/sesion").status_code == 401


# ---------------------------------------------------------------------------- entrar como invitado
def test_invitado_solo_mira(entorno):
    for rol in ["administrador", "analista", "comercio"]:
        invitado = TestClient(entorno["app"])
        assert invitado.get("/api/invitado").json() == {"disponible": True}        # en local, sí
        comercio = entorno["comercio"] if rol == "comercio" else 0
        assert invitado.post("/api/invitado", json={"rol": rol, "comercio_id": comercio}).status_code == 200
        s = invitado.get("/api/sesion").json()
        assert s["rol"] == rol and s["invitado"] is True and s["comercio_id"] == comercio
        assert invitado.get("/api/contexto").status_code == 200                    # mira
        r = invitado.post("/api/perfil", json={"nombre": "Otro nombre"})           # pero no cambia nada
        assert r.status_code == 403 and "invitado" in r.json()["detail"]
        assert invitado.post("/api/logout").status_code == 200                     # salir, sí
    # No salen en la pestaña Cuentas: no son de nadie
    assert not any(c["email"].endswith("@invitado.centinela")
                   for c in entorno["admin"].get("/api/cuentas").json()["cuentas"])


def test_invitado_de_comercio_elige_la_empresa(entorno):
    anonimo = TestClient(entorno["app"])
    empresas = anonimo.get("/api/invitado/comercios").json()
    assert empresas and {"id", "nombre", "categoria", "ciudad", "pagos"} <= set(empresas[0])
    assert anonimo.post("/api/invitado", json={"rol": "comercio"}).status_code == 422          # sin elegir, no
    otra = next(e["id"] for e in empresas if e["id"] != entorno["comercio"])
    anonimo.post("/api/invitado", json={"rol": "comercio", "comercio_id": otra})
    assert {c["id"] for c in anonimo.get("/api/contexto").json()["comercios"]} == {otra}       # solo esa


def test_invitado_apagado(entorno, monkeypatch):
    monkeypatch.setenv("MODO_INVITADO", "0")
    anonimo = TestClient(entorno["app"])
    assert anonimo.get("/api/invitado").json() == {"disponible": False}
    assert anonimo.post("/api/invitado", json={"rol": "administrador"}).status_code == 404
    assert anonimo.get("/api/invitado/comercios").status_code == 404        # la lista de empresas no es pública
