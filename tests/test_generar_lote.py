"""Confirmaciones del banco en los lotes diarios (generar_lote.py): día a día tienen que dar el mismo
retraso que el dataset inicial (dataset.etiquetas en config.yaml)."""
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))   # generar_lote importa dataset
from pipeline.generar_lote import LOG_RETRASO, P_NUNCA_CONFIRMADO, fecha_confirmacion

SEMILLA = 42
MOMENTO = datetime(2026, 9, 1, 13, 45, 10)


def confirmar_dia_a_dia(tid, dias):
    """Simula los lotes diarios desde el día del fraude: devuelve la fecha de confirmación o None."""
    inicio = datetime.combine(MOMENTO.date(), datetime.min.time())
    for _ in range(dias):
        fin = inicio + timedelta(days=1)
        fecha = fecha_confirmacion(tid, MOMENTO, inicio, fin, SEMILLA)
        if fecha is not None:
            return fecha
        inicio = fin
    return None


def test_mismo_fraude_y_mismo_lote_dan_lo_mismo():
    inicio = datetime(2026, 9, 10)
    fin = inicio + timedelta(days=1)
    for tid in range(200):
        assert fecha_confirmacion(tid, MOMENTO, inicio, fin, SEMILLA) == \
            fecha_confirmacion(tid, MOMENTO, inicio, fin, SEMILLA)


def test_la_confirmacion_cae_dentro_del_lote_y_despues_del_fraude():
    inicio = datetime(2026, 9, 1)
    fin = inicio + timedelta(days=1)
    for tid in range(500):
        fecha = fecha_confirmacion(tid, MOMENTO, inicio, fin, SEMILLA)
        if fecha is not None:
            assert MOMENTO <= fecha < fin
    # un fraude posterior al final del lote todavía no puede estar confirmado
    assert all(fecha_confirmacion(tid, fin + timedelta(hours=1), inicio, fin, SEMILLA) is None
               for tid in range(100))


def test_dia_a_dia_sale_el_mismo_retraso_que_en_el_dataset_inicial():
    dias = 90
    fechas = [confirmar_dia_a_dia(tid, dias) for tid in range(1500)]
    retrasos = np.array([(f - MOMENTO).total_seconds() / 86400 for f in fechas if f is not None])
    # esperado: (1 - p_nunca) * P(retraso <= 90 días)
    esperado = (1 - P_NUNCA_CONFIRMADO) * LOG_RETRASO.cdf(math.log(dias))
    assert abs(len(retrasos) / len(fechas) - esperado) < 0.03
    mediana = math.exp(LOG_RETRASO.mean)
    assert abs(np.median(retrasos) - mediana) < 0.15 * mediana
