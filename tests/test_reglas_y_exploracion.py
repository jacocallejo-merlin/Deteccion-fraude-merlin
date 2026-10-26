"""Reglas de alerta (reglas_alerta.py) y presupuesto de exploración de la cola de los analistas
(simular_analistas.elegir_cola). No necesitan ClickHouse."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))


def test_reglas_nuevas_y_card_testing():
    from reglas_alerta import PATRONES_NUEVOS, PLANTILLAS, UMBRALES_INICIALES, construir_reglas
    assert set(PLANTILLAS) == set(UMBRALES_INICIALES)            # cada regla con sus umbrales iniciales
    reglas = {nombre: condicion for _, nombre, _, _, condicion in construir_reglas(UMBRALES_INICIALES)}
    assert "n_comercios_cliente_3h >= 3" in reglas["cash_out_multicomercio"]
    # el primer pago de una ráfaga de card testing (sin pagos previos) también salta si viene de un
    # dispositivo nuevo o con VPN
    assert "dispositivo_nuevo = 1 OR proxy_vpn = 1" in reglas["card_testing"]
    ids = [pid for pid, _, _ in PLANTILLAS.values()]
    assert len(ids) == len(set(ids)) and PLANTILLAS["cash_out_multicomercio"][0] in PATRONES_NUEVOS


def test_el_ajuste_de_umbrales_conoce_todas_las_reglas():
    pytest.importorskip("sklearn")
    from ajustar_umbrales import REJILLAS
    from reglas_alerta import PLANTILLAS
    assert set(PLANTILLAS) <= set(REJILLAS)


def cola(explorar, n=7, umbral=0):
    from simular_analistas import elegir_cola
    # 100 pagos: el recalibrado puntúa alto los primeros; los detectores solos ven más raros los últimos
    info = pd.DataFrame({"transaccion_id": np.arange(100), "score_detectores": np.linspace(0, 1, 100)})
    score = np.linspace(1, 0, 100)
    return elegir_cola(info, score, n, explorar, umbral)


def test_sin_exploracion_la_cola_es_lo_de_mas_puntuacion():
    pytest.importorskip("sklearn")
    c = cola(0)
    assert list(c["transaccion_id"]) == list(range(7)) and not c["explorada"].any()


def test_la_exploracion_revisa_lo_que_ven_raro_los_detectores():
    pytest.importorskip("sklearn")
    c = cola(0.15)
    assert len(c) == 7 and c["explorada"].sum() == 1                         # 15 % de 7 = 1
    assert list(c.loc[~c["explorada"], "transaccion_id"]) == list(range(6))   # el resto, lo de más puntuación
    assert c.loc[c["explorada"], "transaccion_id"].item() == 99              # lo más raro para los detectores
    # con un presupuesto de 1 al día no se quita la única revisión principal
    assert not cola(0.15, n=1)["explorada"].any()


def test_el_umbral_minimo_no_afecta_a_la_exploracion():
    pytest.importorskip("sklearn")
    c = cola(0.15, umbral=0.98)
    assert c.loc[c["explorada"], "transaccion_id"].item() == 99
    assert (c.loc[~c["explorada"], "puntuacion"] >= 0.98).all()
