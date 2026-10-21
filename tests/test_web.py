"""Las dos webs responden con los datos del pipeline.

- web/app.py (casos, resumen, patrones, pagos, avisos): necesita ClickHouse con el pipeline ejecutado
  hasta la simulación de analistas (tabla riesgo_transaccion). Si no está, se salta.
- web_modelos/app.py (informe de los modelos): lee modelos/, datos_ml/ y resultados/. Si el pipeline
  no los ha generado (p. ej. en el CI, que solo hace el ETL), se salta.
Solo se prueban rutas de lectura: las que escriben (revisiones, avisos leídos) no se tocan.
"""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def web(clickhouse):
    if not clickhouse.command("EXISTS TABLE riesgo_transaccion"):
        pytest.skip("riesgo_transaccion no existe: ejecuta el pipeline completo (main.py)")
    from web.app import app
    cliente = TestClient(app)
    if cliente.get("/api/contexto").status_code != 200:
        pytest.skip("la web no tiene datos todavía")
    return cliente


@pytest.fixture(scope="module")
def web_modelos():
    from web_modelos.app import app
    cliente = TestClient(app)
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
def test_web_modelos(web_modelos):
    assert web_modelos.get("/").status_code == 200
    d = web_modelos.get("/api/modelos").json()
    claves = {m["clave"] for m in d["modelos"]}
    assert claves <= {"kmeans", "iforest", "ocsvm", "dbscan", "autoencoder"} and claves
    assert sum(m["elegido"] for m in d["modelos"]) == 1
    assert all(0 <= m["encontrados"] <= d["fraudes"] for m in d["modelos"])
