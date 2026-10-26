"""El recalibrador del Hito 6 con varios detectores: la media de percentiles como respaldo y la
compatibilidad de los recalibradores antiguos. No necesita ClickHouse ni los modelos entrenados."""
import sys
from datetime import datetime
from pathlib import Path

import joblib
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))
import recalibrar_modelo as rm


def test_score_sin_recalibrar_es_la_media_de_detectores():
    X = pd.DataFrame({'pct_kmeans': [0.2, 0.8], 'pct_autoencoder': [0.4, 0.6],
                      'importe': [10.0, 20.0]})
    assert rm.score_sin_recalibrar(X).tolist() == pytest.approx([0.3, 0.7])


def test_score_sin_recalibrar_con_un_detector_es_su_percentil(monkeypatch):
    monkeypatch.setattr(rm, 'COLUMNAS_DETECTORES', ['pct_kmeans'])
    X = pd.DataFrame({'pct_kmeans': [0.2, 0.8]})
    assert rm.score_sin_recalibrar(X).tolist() == pytest.approx([0.2, 0.8])


def test_cargar_recalibrador_de_varios_detectores(monkeypatch, tmp_path):
    ruta = tmp_path / 'recalibrador.joblib'
    monkeypatch.setattr(rm, 'RUTA_RECALIBRADOR', ruta)
    monkeypatch.setattr(rm, 'run_ids_base', lambda: {'kmeans': 'k1', 'autoencoder': 'a1'})
    monkeypatch.setattr(rm, 'variables_extra_en', lambda corte: [])
    monkeypatch.setattr(rm, 'variables_quitadas_en', lambda corte: [])
    recal = {'detectores': rm.DETECTORES, 'run_ids_base': {'kmeans': 'k1', 'autoencoder': 'a1'},
             'corte': datetime(2026, 1, 1)}
    joblib.dump(recal, ruta)

    assert rm.cargar_recalibrador() == recal

    recal['run_ids_base']['autoencoder'] = 'a2'
    joblib.dump(recal, ruta)
    assert rm.cargar_recalibrador() is None


def test_recalibrador_antiguo_sigue_valiendo_con_un_detector(monkeypatch, tmp_path):
    ruta = tmp_path / 'recalibrador.joblib'
    monkeypatch.setattr(rm, 'RUTA_RECALIBRADOR', ruta)
    monkeypatch.setattr(rm, 'DETECTORES', ['kmeans'])
    monkeypatch.setattr(rm, 'MODELO_BASE', 'kmeans')
    monkeypatch.setattr(rm, 'run_id_detector', lambda nombre: 'k1')
    monkeypatch.setattr(rm, 'variables_extra_en', lambda corte: [])
    monkeypatch.setattr(rm, 'variables_quitadas_en', lambda corte: [])
    recal = {'run_id_base': 'k1', 'corte': datetime(2026, 1, 1)}
    joblib.dump(recal, ruta)

    assert rm.cargar_recalibrador() == recal
