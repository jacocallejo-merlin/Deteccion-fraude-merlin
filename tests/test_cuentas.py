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
    """Las cuentas de prueba van en un SQLite temporal (conftest.py), pero los comercios que se registran en los
    tests sí tienen su fila en la tabla comercio de ClickHouse: se quitan para no dejarla con empresas de prueba."""
    from web.sesiones import Almacen
    almacen = Almacen()
    registrados = [c["comercio_id"] for c in almacen.cuentas()
                   if c["email"].endswith("@tests.centinela") and c["revisada_por"] == "registro"]
    if registrados:
        clickhouse.command("ALTER TABLE comercio DELETE WHERE comercio_id IN {c:Array(UInt32)} "
                           "SETTINGS mutations_sync = 1", parameters={"c": registrados})
        almacen.borrar_empresas(registrados)
    almacen.borrar_usuarios("@tests.centinela")


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
    # En el CI solo está el ETL: las rutas de datos (/api/contexto...) necesitan el scoring. Lo que se ve con datos
    # se comprueba solo si los hay (y entero en test_web.py); el acceso, con /api/perfil, que solo usa las cuentas
    scoring = bool(clickhouse.command("EXISTS TABLE riesgo_transaccion"))
    yield {"app": app, "almacen": _almacen, "admin": admin, "analista": analista, "email": email,
           "comercio": comercio, "clickhouse": clickhouse, "scoring": scoring}
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
EMPRESA = {"nombre": "Tienda Tests", "cif": "b-1234567-4", "categoria": "moda", "pais": "España",
           "ciudad": "Vigo", "telefono": "986 000 000"}


def test_registro_de_un_comercio(entorno):
    from datetime import timedelta
    from web.cuentas import hoy_madrid
    email = entorno["email"]("tienda")
    nuevo = TestClient(entorno["app"])
    cuenta = {"nombre": "Laura Gil", "email": email, "contrasena": CONTRASENA}
    # Paso 1: la cuenta. Paso 2: la empresa y su tamaño (lo que se puede elegir lo da /api/registro/opciones)
    assert nuevo.post("/api/registro/comprobar", json=cuenta).json()["ok"] is True
    opciones = nuevo.get("/api/registro/opciones").json()
    assert "moda" in opciones["sectores"] and [t["clave"] for t in opciones["tamanos"]] == ["pequeno", "mediano", "grande"]
    for campo in ["nombre", "cif", "telefono", "ciudad"]:                    # la empresa se valida antes del código
        r = nuevo.post("/api/registro", json={**cuenta, "empresa": {**EMPRESA, campo: ""}})
        assert r.status_code == 422, campo
    assert nuevo.post("/api/registro", json={**cuenta, "empresa": {**EMPRESA, "cif": "B12345675"}}).status_code == 422
    codigo = nuevo.post("/api/registro", json={**cuenta, "empresa": EMPRESA, "tamano": "mediano"}).json()["codigo_demo"]
    assert entrar(nuevo, email).status_code == 401                          # sin confirmar el correo, no existe
    # Paso 3: el código. Se crean la cuenta y la empresa, ya configurada y dada de alta hoy
    r = nuevo.post("/api/registro/confirmar", json={"email": email, "codigo": codigo})
    assert r.status_code == 200 and r.json()["rol"] == "comercio"
    s = nuevo.get("/api/sesion").json()
    assert s["comercio_id"] >= 100_000 and s["comercio_nombre"] == "Tienda Tests"   # comercio nuevo, de cero
    e = nuevo.get("/api/empresa").json()
    assert e["configurada"] is True and e["registrada"] is True and e["pagos"] == 0 and e["sectores"]
    assert e["cif"] == "B12345674" and e["categoria"] == "moda"              # limpio: sin guiones y en mayúsculas
    hoy = hoy_madrid()
    assert e["alta"] == str(hoy) and e["primeros_pagos"] == str(hoy + timedelta(days=1))   # pagos, desde mañana
    assert e["tamano"] == "mediano" and e["pagos_dia"] > 0
    # Mi empresa: se pueden cambiar los datos (con la misma validación) sin perder el alta
    for campo in ["nombre", "cif", "telefono", "ciudad"]:                    # obligatorios
        assert nuevo.post("/api/empresa", json={**EMPRESA, campo: ""}).status_code == 422, campo
    assert nuevo.post("/api/empresa", json={**EMPRESA, "telefono": "1234"}).status_code == 422
    e = nuevo.post("/api/empresa", json={**EMPRESA, "nombre": "Tienda Tests Vigo"}).json()
    assert e["alta"] == str(hoy) and e["tamano"] == "mediano"
    assert nuevo.get("/api/sesion").json()["comercio_nombre"] == "Tienda Tests Vigo"   # el nombre, también en comercio
    # Solo ve lo suyo: ni los demás comercios ni la gestión de cuentas
    if entorno["scoring"]:
        assert [c["id"] for c in nuevo.get("/api/contexto").json()["comercios"]] == [s["comercio_id"]]
        assert nuevo.get("/api/transacciones", params={"comercio": 0}).json() == []
    assert nuevo.get("/api/cuentas").status_code == 403
    assert entrar(TestClient(entorno["app"]), email).status_code == 200    # y entra con su contraseña


def test_registro_no_valido(entorno):
    anonimo = TestClient(entorno["app"])
    cuenta = {"nombre": "Otro", "email": entorno["email"]("otra"), "contrasena": CONTRASENA}
    comprobar = lambda **c: anonimo.post("/api/registro/comprobar", json={**cuenta, **c}).status_code   # noqa: E731
    assert comprobar(contrasena="corta") == 422
    assert comprobar(contrasena="sinsimbolo123") == 422                     # sin símbolo
    assert comprobar(email="no-es-un-correo") == 422
    assert comprobar(email=entorno["email"]("analista")) == 409             # ya tiene cuenta
    # El registro entero vuelve a comprobar la cuenta (aunque alguien se salte el paso 1)
    assert anonimo.post("/api/registro", json={**cuenta, "contrasena": "corta", "empresa": EMPRESA}).status_code == 422
    assert anonimo.post("/api/registro", json={**cuenta, "empresa": EMPRESA, "tamano": "enorme"}).status_code == 422
    anonimo.post("/api/registro", json={**cuenta, "empresa": EMPRESA})
    assert anonimo.post("/api/registro/confirmar", json={"email": cuenta["email"], "codigo": "12345"}).status_code == 422


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
    r = nuevo.get("/api/perfil")
    assert r.status_code == 403 and r.json()["cambiar_contrasena"] is True
    assert nuevo.get("/", follow_redirects=False).headers["location"] == "/login?cambiar=1"
    cambio = {"actual": provisional, "nueva": provisional}
    assert nuevo.post("/api/perfil/contrasena", json=cambio).status_code == 422    # tiene que ser otra
    assert nuevo.post("/api/perfil/contrasena", json={**cambio, "nueva": "mi-contrasena-1"}).status_code == 200
    assert nuevo.get("/api/perfil").status_code == 200
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
        assert invitado.get("/api/perfil").status_code == 200                      # mira
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
    assert anonimo.get("/api/sesion").json()["comercio_id"] == otra
    if entorno["scoring"]:
        assert {c["id"] for c in anonimo.get("/api/contexto").json()["comercios"]} == {otra}   # solo esa


def test_invitado_apagado(entorno, monkeypatch):
    monkeypatch.setenv("MODO_INVITADO", "0")
    anonimo = TestClient(entorno["app"])
    assert anonimo.get("/api/invitado").json() == {"disponible": False}
    assert anonimo.post("/api/invitado", json={"rol": "administrador"}).status_code == 404
    assert anonimo.get("/api/invitado/comercios").status_code == 404        # la lista de empresas no es pública
