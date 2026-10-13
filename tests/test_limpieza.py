from datetime import datetime, timezone

import numpy as np

from pipeline.dataset import (COLUMNAS_FORMATO, FORMATO_FECHA, TABLAS_DUPLICADOS, Fabrica, ensuciar,
                              fecha_bruta)
from pipeline.limpieza import elegir_canonicos

SIN_SUCIEDAD = {"p_duplicado": 0, "p_formato": 0, "p_fecha_otro_formato": 0, "p_fecha_hora_local": 0,
                "p_fecha_ilegible": 0, "p_otra_divisa": 0, "tipos_cambio": {"USD": 1.10, "GBP": 0.85}}


def test_variantes_vuelven_a_la_forma_mayoritaria():
    conteos = {"España": 900, "españa": 5, " España": 3, "ESPAÑA ": 2, "Francia": 100}
    assert elegir_canonicos(conteos) == {"españa": "España", " España": "España", "ESPAÑA ": "España"}


def test_valor_unico_con_espacios_se_recorta():
    assert elegir_canonicos({" Egipto": 1}) == {" Egipto": "Egipto"}


def test_sin_variantes_no_cambia_nada():
    assert elegir_canonicos({"aprobada": 10, "rechazada": 2}) == {}


def fabrica_pequena():
    fab = Fabrica()
    momento = datetime(2026, 3, 1, 10, 30)
    paises = ["España"] * 80 + ["Francia"] * 20
    for i, pais in enumerate(paises, 1):
        fab.tablas["cliente"].append({"cliente_id": i, "pais": pais, "email": f"c{i}@correo.es"})
        fab.tablas["sesion"].append({"sesion_id": i, "ip_pais": pais, "timestamp": momento})
        fab.tablas["evento"].append({"evento_id": i, "sesion_id": i, "timestamp": momento})
        fab.tablas["transaccion"].append({"transaccion_id": i, "estado": "aprobada", "moneda": "EUR",
                                          "metodo_autenticacion": "3D-secure", "timestamp": momento,
                                          "cantidad": 10.0 + i})
    fab.tablas["comercio"].append({"comercio_id": 1, "pais": "España"})
    return fab


def test_ensuciar_y_limpiar_textos_recupera_los_datos_originales():
    fab = fabrica_pequena()
    originales = {t: [dict(f) for f in filas] for t, filas in fab.tablas.items()}
    ensuciar(fab, semilla=0, s={**SIN_SUCIEDAD, "p_duplicado": 0.1, "p_formato": 0.2})
    for t in originales["transaccion"]:
        t["timestamp"] = t["timestamp"].strftime(FORMATO_FECHA)   # llega como texto

    # hay suciedad de los dos tipos
    assert any(len(fab.tablas[t]) > len(originales[t]) for t in TABLAS_DUPLICADOS)
    assert any(f != o for f, o in zip(fab.tablas["cliente"], originales["cliente"]))

    # quitar duplicados exactos + normalizar formatos deja la tabla como estaba
    for tabla, columnas in COLUMNAS_FORMATO.items():
        filas = list({tuple(sorted(f.items())): f for f in fab.tablas[tabla]}.values())
        for col in columnas:
            if col == "email":
                for f in filas:
                    f[col] = f[col].strip().lower()
                continue
            conteos = {}
            for f in filas:
                conteos[f[col]] = conteos.get(f[col], 0) + 1
            cambios = elegir_canonicos(conteos)
            for f in filas:
                f[col] = cambios.get(f[col], f[col])
        assert filas == originales[tabla], tabla


def test_divisas_se_recuperan_al_centimo():
    fab = fabrica_pequena()
    originales = [t["cantidad"] for t in fab.tablas["transaccion"]]
    s = {**SIN_SUCIEDAD, "p_otra_divisa": 0.5}
    ensuciar(fab, semilla=0, s=s)
    convertidas = [t for t in fab.tablas["transaccion"] if t["moneda"] != "EUR"]
    assert convertidas
    for t in convertidas:   # lo mismo que hace limpieza.py en SQL
        euros = round(t["cantidad"] / s["tipos_cambio"][t["moneda"]], 2)
        assert abs(euros - originales[t["transaccion_id"] - 1]) <= 0.011


def test_fechas_brutas_representan_el_mismo_instante():
    rng = np.random.default_rng(0)
    momento = datetime(2026, 7, 5, 11, 22, 33)   # horario de verano: Madrid = UTC+2
    s = {**SIN_SUCIEDAD, "p_fecha_otro_formato": 0.3, "p_fecha_hora_local": 0.3}
    vistos = set()
    for _ in range(200):
        texto, tipo = fecha_bruta(momento, rng, s)
        vistos.add(tipo)
        if tipo is None:
            leida = datetime.strptime(texto, FORMATO_FECHA)
        elif tipo == "dd/mm/aaaa":
            leida = datetime.strptime(texto, "%d/%m/%Y %H:%M:%S")
        else:
            assert texto.endswith("+02:00")
            leida = datetime.fromisoformat(texto).astimezone(timezone.utc).replace(tzinfo=None)
        assert leida == momento
    assert vistos == {None, "dd/mm/aaaa", "hora de Madrid con desfase"}
