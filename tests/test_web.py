"""Las dos webs responden con los datos del pipeline.

- web/app.py (casos, resumen, patrones, pagos, avisos): necesita ClickHouse con el pipeline ejecutado
  hasta la simulación de analistas (tabla riesgo_transaccion). Si no está, se salta.
- web_modelos/app.py (informe de los modelos, solo el administrador): lee modelos/, datos_ml/ y resultados/ (y
  ClickHouse para la sesión). Si el pipeline no los ha generado (p. ej. en el CI, que solo hace el ETL), se salta.
Las dos piden sesión: se crean tres cuentas de prueba (administrador, analista y comercio) y se borran al acabar.
Solo se prueban rutas de lectura: las que escriben (revisiones, avisos leídos) no se tocan.
"""
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

CONTRASENA = "prueba-de-tests-123"


@pytest.fixture(scope="module")
def cuentas(clickhouse):
    """Una cuenta de cada tipo (el comercio, el primero), solo para los tests."""
    from web.sesiones import Almacen
    almacen = Almacen()               # el SQLite temporal de los tests (conftest.py)
    comercio = clickhouse.query("SELECT min(comercio_id) FROM comercio").result_rows[0][0]
    emails = {"administrador": "admin@tests.centinela", "analista": "analista@tests.centinela",
              "comercio": "comercio@tests.centinela"}
    almacen.guardar_usuario(emails["administrador"], "Tests admin", "administrador", 0, CONTRASENA)
    almacen.guardar_usuario(emails["analista"], "Tests", "analista", 0, CONTRASENA)
    almacen.guardar_usuario(emails["comercio"], "Tests comercio", "comercio", comercio, CONTRASENA)
    yield {**emails, "comercio_id": comercio, "almacen": almacen}
    from test_cuentas import borrar_cuentas_de_prueba
    borrar_cuentas_de_prueba(clickhouse)


def entrar(cliente, email):
    r = cliente.post("/api/login", json={"email": email, "contrasena": CONTRASENA})
    assert r.status_code == 200, r.text[:200]
    return cliente


@pytest.fixture(scope="module")
def web(clickhouse, cuentas):
    if not clickhouse.command("EXISTS TABLE riesgo_transaccion"):
        pytest.skip("riesgo_transaccion no existe: ejecuta el pipeline completo (main.py)")
    from web.app import app
    cliente = entrar(TestClient(app), cuentas["analista"])
    if cliente.get("/api/contexto").status_code != 200:
        pytest.skip("la web no tiene datos todavía")
    return cliente


@pytest.fixture(scope="module")
def web_comercio(web, cuentas):
    from web.app import app
    return entrar(TestClient(app), cuentas["comercio"])


@pytest.fixture(scope="module")
def web_modelos(cuentas):
    from web_modelos.app import app
    cliente = TestClient(app)
    # La web de modelos usa la sesión de la de casos (misma cookie); aquí se abre directamente
    cliente.cookies.set("centinela_sesion", cuentas["almacen"].abrir_sesion(cuentas["administrador"], timedelta(hours=1)))
    r = cliente.get("/api/modelos")
    if r.status_code == 503:
        pytest.skip(f"faltan los resultados de los modelos: {r.json().get('detail', '')[:100]}")
    return cliente


# ---------------------------------------------------------------------------- web de casos
def test_pagina_y_estaticos(web):
    for ruta in ["/", "/static/app.js", "/static/patrones.js", "/static/estilos.css", "/static/chart.umd.min.js"]:
        assert web.get(ruta).status_code == 200, ruta


def test_salud(web):
    """La sonda de Kubernetes (k8s/web.yaml) responde 200 con ClickHouse levantado."""
    assert web.get("/api/salud").json() == {"estado": "ok"}


@pytest.mark.parametrize("dias", [0, 7, 30])
def test_rutas_con_periodo(web, dias):
    for ruta in ["/api/resumen", "/api/patrones"]:
        r = web.get(ruta, params={"dias": dias})
        assert r.status_code == 200, (ruta, r.text[:200])
    for estado in ["pendientes", "revisadas", "todas"]:
        r = web.get("/api/alertas", params={"dias": dias, "estado": estado})
        assert r.status_code == 200, (estado, r.text[:200])


def test_rutas_sin_periodo(web):
    for ruta, params in [("/api/contexto", {}), ("/api/dia", {}), ("/api/avisos", {"limite": 10}),
                         ("/api/transacciones", {"limite": 10}), ("/api/transacciones", {"limite": 10, "orden": "riesgo"})]:
        r = web.get(ruta, params=params)
        assert r.status_code == 200, (ruta, r.text[:200])
    assert len(web.get("/api/transacciones", params={"limite": 10}).json()) <= 10


def test_vista_de_un_comercio(web):
    comercio = web.get("/api/contexto").json()["comercios"][0]["id"]
    for ruta in ["/api/resumen", "/api/patrones", "/api/alertas"]:
        assert web.get(ruta, params={"comercio": comercio}).status_code == 200, ruta


def test_sin_sesion_no_se_ve_nada(web):
    from web.app import app
    anonimo = TestClient(app)
    assert anonimo.get("/api/contexto").status_code == 401
    r = anonimo.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert anonimo.get("/login").status_code == 200 and anonimo.get("/api/salud").status_code == 200
    assert anonimo.post("/api/login", json={"email": "analista@tests.centinela", "contrasena": "mala"}).status_code == 401


def test_un_comercio_solo_ve_lo_suyo(web_comercio, cuentas):
    """Aunque la página pida otro comercio (o todos), el servidor le da solo el suyo."""
    suyo = cuentas["comercio_id"]
    assert web_comercio.get("/api/sesion").json()["rol"] == "comercio"
    assert [c["id"] for c in web_comercio.get("/api/contexto").json()["comercios"]] == [suyo]
    pagos = web_comercio.get("/api/transacciones", params={"comercio": 0, "limite": 50, "demo": True}).json()
    assert all(p["comercio_id"] == suyo for p in pagos)
    assert all("real" not in p for p in pagos)                    # sin modo demostración
    assert web_comercio.get("/api/patrones", params={"comercio": 0}).json()["por_comercio"] == []


def test_avisos_de_un_comercio_sin_los_otros(web_comercio, web, cuentas):
    """Un aviso es de una tarjeta, que puede haber pagado en varios comercios: el comercio solo ve lo suyo."""
    avisos = web_comercio.get("/api/avisos", params={"limite": 500}).json()["avisos"]
    if not avisos:
        pytest.skip("este comercio no tiene avisos")
    otros = [c["nombre"] for c in web.get("/api/contexto").json()["comercios"] if c["id"] != cuentas["comercio_id"]]
    for a in avisos:
        assert "en tu comercio" in a["mensaje"], a["mensaje"]
        assert not any(nombre in a["mensaje"] for nombre in otros), a["mensaje"]
        assert a["transacciones"] >= 1


def test_patrones_cuadran(web):
    """Los repartos suman el total, nunca hay más fraudes que pagos y la vista de un comercio no
    compara comercios."""
    p = web.get("/api/patrones", params={"dias": 0}).json()
    assert p["pagos"] > 0 and 0 <= p["fraudes"] <= p["pagos"]
    for reparto in ["por_hora", "por_dia_semana", "por_dispositivo"]:
        assert sum(f["pagos"] for f in p[reparto]) == p["pagos"], reparto
        assert sum(f["fraudes"] for f in p[reparto]) == p["fraudes"], reparto
        assert all(0 <= f["fraudes"] <= f["pagos"] for f in p[reparto]), reparto
    assert {f["hora"] for f in p["por_hora"]} <= set(range(24))
    assert len(p["clientes"]) <= 8 and all(c["fraudes"] > 0 for c in p["clientes"])
    assert p["clientes_con_fraude"] <= p["clientes_activos"]
    comercio = web.get("/api/contexto").json()["comercios"][0]["id"]
    assert web.get("/api/patrones", params={"comercio": comercio}).json()["por_comercio"] == []


def test_ficha_de_un_caso(web):
    casos = web.get("/api/alertas", params={"dias": 0, "estado": "todas"}).json().get("casos", [])
    if not casos:
        pytest.skip("no hay casos de alerta")
    c = casos[0]
    r = web.get("/api/caso", params={"metodo_id": c["metodo_id"], "dia": str(c["dia"])})
    assert r.status_code == 200, r.text[:200]


def test_entradas_no_validas(web):
    assert web.get("/api/alertas", params={"estado": "x"}).status_code == 422
    assert web.get("/api/patrones", params={"dias": -1}).status_code == 422
    assert web.get("/api/transacciones", params={"orden": "x"}).status_code == 422


# -------------------------------------------------------------------------- web de modelos
def test_web_modelos_solo_el_administrador(web_modelos, cuentas):
    from web_modelos.app import app
    anonimo = TestClient(app)
    assert anonimo.get("/api/modelos").status_code == 401
    for rol in ["comercio", "analista"]:
        otro = TestClient(app)
        otro.cookies.set("centinela_sesion", cuentas["almacen"].abrir_sesion(cuentas[rol], timedelta(hours=1)))
        assert otro.get("/api/modelos").status_code == 403, rol


def test_web_modelos(web_modelos):
    assert web_modelos.get("/").status_code == 200
    d = web_modelos.get("/api/modelos").json()
    claves = {m["clave"] for m in d["modelos"]}
    assert claves <= {"kmeans", "iforest", "ocsvm", "dbscan", "gmm", "autoencoder", "hgb_gmm"} and claves
    assert sum(m["elegido"] for m in d["modelos"]) == 1
    assert all(0 <= m["encontrados"] <= d["fraudes"] for m in d["modelos"])
    # El supervisado sale, pero nunca es el elegido ni el mejor de la prueba previa: no compite
    assert not any(m["elegido"] for m in d["modelos"] if m["supervisado"])
    assert d["mejor_validacion"] != "hgb_gmm"
