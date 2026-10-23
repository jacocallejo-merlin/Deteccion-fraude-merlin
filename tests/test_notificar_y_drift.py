"""Notificaciones (pipeline/notificar.py), PSI de la vigilancia (pipeline/vigilar_drift.py) y límite de
ficheros de resultados/ (artefactos.py). No necesitan ClickHouse ni enviar nada de verdad."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import notificar
from vigilar_drift import psi

VARIABLES = ["TEAMS_WEBHOOK_URL", "SLACK_WEBHOOK_URL", "SMTP_SERVIDOR", "CORREO_DESTINATARIOS", "BREVO_API_KEY"]


@pytest.fixture
def sin_canales(monkeypatch):
    for v in VARIABLES:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setitem(notificar._cfg, "correo", {})
    monkeypatch.setattr(notificar, "usuarios", lambda: [])        # sin usuarios de la web


@pytest.fixture
def envios(monkeypatch, sin_canales):
    """Captura lo que se mandaría a Teams y Slack en vez de enviarlo."""
    capturados = []
    monkeypatch.setattr(notificar, "_post_json", lambda url, cuerpo: capturados.append((url, cuerpo)) or 200)
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://teams.ejemplo/flujo")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://slack.ejemplo/hook")
    return capturados


def test_psi_estable_y_desviado():
    rng = np.random.default_rng(0)
    referencia = rng.normal(0, 1, 20_000)
    assert psi(referencia, rng.normal(0, 1, 5_000)) < 0.02          # misma distribución
    assert psi(referencia, rng.normal(1, 1, 5_000)) > 0.25          # desplazada una desviación típica


def test_psi_binaria_y_nulos():
    rng = np.random.default_rng(1)
    binaria = rng.random(20_000) < 0.1
    assert psi(binaria, rng.random(5_000) < 0.1) < 0.02
    assert psi(binaria, rng.random(5_000) < 0.5) > 0.25              # de 10 % a 50 % de unos
    con_nulos = np.where(rng.random(5_000) < 0.5, np.nan, rng.normal(0, 1, 5_000))
    assert psi(rng.normal(0, 1, 20_000), con_nulos) > 0.25           # la mitad deja de llegar


def test_sin_canales_no_envia(sin_canales):
    assert notificar.canales() == {}
    assert notificar.notificar("titulo", "texto") == 0


def test_formato_teams_y_slack(envios):
    assert notificar.notificar("Falla X", "detalle", "error", ["línea 1"]) == 2
    (_, teams), (_, slack) = sorted(envios, key=lambda e: "slack" in e[0])
    tarjeta = teams["attachments"][0]
    assert tarjeta["contentType"] == "application/vnd.microsoft.card.adaptive"
    textos = [b["text"] for b in tarjeta["content"]["body"]]
    assert textos[0] == "Falla X" and "- línea 1" in textos
    assert slack["text"].startswith("*Falla X*") and "• línea 1" in slack["text"]


def test_tipo_apagado_no_envia(envios, monkeypatch):
    monkeypatch.setitem(notificar._cfg, "drift", False)
    assert notificar.notificar("t", "x", tipo="drift") == 0 and not envios


def test_resumen_de_avisos_en_un_mensaje(envios, monkeypatch):
    """Muchos avisos (la primera ejecución crea los de todo el histórico) van en UN mensaje por canal,
    con los críticos primero y solo max_lineas en detalle."""
    monkeypatch.setitem(notificar._cfg, "max_lineas", 3)
    avisos = [{"dia": f"2026-08-{d:02d}", "nivel": "alto", "mensaje": f"caso {d}"} for d in range(1, 21)]
    avisos.append({"dia": "2026-08-01", "nivel": "critico", "mensaje": "el grave"})
    notificar.notificar_avisos(avisos)
    slack = next(c for u, c in envios if "slack" in u)["text"].splitlines()
    assert len(envios) == 2
    assert "21 avisos" in slack[0] and "1 crítico" in slack[0]
    assert "[CRITICO] el grave" in slack[2]                    # el crítico primero aunque sea antiguo
    assert "2026-08-20" in slack[3]                            # después, los más recientes
    assert "18 más" in slack[-1]
    assert notificar.notificar_avisos([]) == 0


def test_avisos_a_cada_usuario(envios, monkeypatch):
    """Cada usuario recibe en su Teams lo suyo: el analista todos los avisos y los del sistema; el comercio,
    solo los de sus pagos y sin los nombres de otros comercios; quien los apagó, nada."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL")
    def usuario(email, rol, comercio, **ajustes):
        return {"email": email, "rol": rol, "comercio_id": comercio,
                **notificar.ajustes_de(rol, {"teams_webhook_url": f"https://teams.ejemplo/{email}", **ajustes})}
    monkeypatch.setattr(notificar, "usuarios", lambda: [
        usuario("ana", "analista", 0), usuario("tienda", "comercio", 12),
        usuario("callado", "analista", 0, avisos_fraude="0", sistema="0")])
    avisos = [{"dia": "2026-08-02", "nivel": "critico", "comercios": [12, 30],
               "mensaje": "Tarjeta 1234: 2 pagos sospechosos por 90 € en 2 comercios (Moda Sol, Otra)."},
              {"dia": "2026-08-02", "nivel": "alto", "comercios": [30], "mensaje": "Tarjeta 9999: de otra tienda."}]
    assert notificar.notificar_avisos(avisos) == 3               # servidor, ana y tienda
    textos = {u: [b["text"] for b in c["attachments"][0]["content"]["body"]] for u, c in envios}
    assert "2 avisos" in textos["https://teams.ejemplo/ana"][0]
    tienda = textos["https://teams.ejemplo/tienda"]
    assert "1 aviso" in tienda[0] and "Tarjeta 1234: pagos sospechosos en tu comercio" in tienda[2]
    assert not any("Otra" in t for t in tienda)
    envios.clear()
    assert notificar.notificar("Drift", "x", tipo="drift") == 2    # servidor y ana (el comercio no)
    assert sorted(u for u, _ in envios) == ["https://teams.ejemplo/ana", "https://teams.ejemplo/flujo"]


def test_limpiar_ficheros(tmp_path):
    from artefactos import limpiar_ficheros
    for i in range(15):
        (tmp_path / f"validacion_202610{i:02d}_1200.txt").write_text("x")
    (tmp_path / "otro.txt").write_text("x")
    assert limpiar_ficheros(tmp_path, "validacion_*.txt", 10) == 5
    quedan = sorted(p.name for p in tmp_path.glob("validacion_*.txt"))
    assert len(quedan) == 10 and quedan[0] == "validacion_20261005_1200.txt"
    assert (tmp_path / "otro.txt").exists()


def test_correo_por_brevo(sin_canales, monkeypatch):
    """Con BREVO_API_KEY, el correo va por la API de Brevo (HTTPS), que también funciona en Render."""
    peticiones = []

    class Respuesta:
        status = 201
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(notificar.urllib.request, "urlopen", lambda p, timeout: peticiones.append(p) or Respuesta())
    monkeypatch.setenv("BREVO_API_KEY", "xkeysib-prueba")
    monkeypatch.setenv("CORREO_REMITENTE", "centinela@ejemplo.com")
    assert notificar.a_correo("ana@banco.com", "Tu código", "Tu código es 123456") is True
    (p,) = peticiones
    cuerpo = __import__("json").loads(p.data)
    assert p.full_url == "https://api.brevo.com/v3/smtp/email" and p.get_header("Api-key") == "xkeysib-prueba"
    assert cuerpo["to"] == [{"email": "ana@banco.com"}] and cuerpo["sender"]["email"] == "centinela@ejemplo.com"
    assert "123456" in cuerpo["textContent"]
