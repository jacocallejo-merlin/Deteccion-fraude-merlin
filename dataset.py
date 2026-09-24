# -*- coding: utf-8 -*-
"""
Genera datos sintéticos de 6 meses para las 12 tablas de `fraude_pagos`,
con los 9 tipos de fraude que definimos inyectados aleatoriamente.

Diseño clave: como este es un dataset SINTÉTICO donde nosotros decidimos
qué es fraude, la etiqueta de verdad-terreno vive en la tabla `alerta`:
toda `transaccion_id` que aparece en `alerta` es un caso fraudulento
inyectado a propósito (y `patron_id` dice de qué tipo). Para entrenar,
un LEFT JOIN transaccion-alerta y comprobar si hay fila es tu "es_fraude".

Uso:
    python generar_dataset.py                # genera y carga en ClickHouse
    python generar_dataset.py --dry-run       # genera en memoria, no conecta a nada
    python generar_dataset.py --seed 7        # reproducible con otra semilla
    python generar_dataset.py --dias 90       # dataset más corto para probar rápido
"""
import argparse
import random
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd

# ------------------------------------------------------------------ #
# Configuración de conexión — igual que BD_VACIAS.py
# ------------------------------------------------------------------ #
HOST = "localhost"
PORT = 8123
USER = "default"
PASSWORD = "password"
DATABASE = "fraude_pagos"

# ------------------------------------------------------------------ #
# Volumen del dataset (ajustable)
# ------------------------------------------------------------------ #
N_CLIENTES = 2000
N_COMERCIOS = 30
N_DISPOSITIVOS = 2600
TRANSACCIONES_DIA_MEDIA = 200      # media de Poisson, transacciones legítimas/día
TASA_FRAUDE_OBJETIVO = 0.02        # ~2% de las transacciones totales

# Reparto del fraude por tipo (en proporción de TRANSACCIONES fraudulentas,
# no de "casos" — un caso de card testing genera varias transacciones)
PROPORCION_FRAUDE = {
    "pico_gasto":        0.25,
    "card_testing":       0.20,
    "smurfing":           0.15,
    "account_takeover":   0.15,
    "ip_sospechosa":      0.10,
    "dispositivo_nuevo":  0.08,
    "devolucion_abusiva": 0.04,
    "bot":                0.02,
    "bust_out":           0.01,
}
# Transacciones que genera, en promedio, UN caso de cada tipo
TXNS_POR_CASO = {
    "pico_gasto": 1, "card_testing": 8, "smurfing": 5, "account_takeover": 1,
    "ip_sospechosa": 1, "dispositivo_nuevo": 1, "devolucion_abusiva": 1,
    "bot": 1, "bust_out": 1,
}

PAISES_HABITUALES = ["España", "Francia", "Alemania", "Portugal", "Italia", "Reino Unido"]
PAISES_HABITUALES_PESOS = [0.55, 0.12, 0.10, 0.08, 0.08, 0.07]
PAISES_RAROS = ["Rusia", "Nigeria", "Vietnam", "Ucrania", "Indonesia", "Filipinas"]

NOMBRES = ["Marta", "Carlos", "Laura", "David", "Sofía", "Pablo", "Elena", "Javier",
           "Lucía", "Diego", "Ana", "Sergio", "Claudia", "Mario", "Irene", "Hugo"]
APELLIDOS = ["García", "Martínez", "López", "Fernández", "Pérez", "González", "Sánchez",
             "Romero", "Torres", "Ramírez", "Flores", "Díaz", "Ortega", "Molina"]


# ------------------------------------------------------------------ #
# Contenedor con contadores de ID y las listas de filas a insertar
# ------------------------------------------------------------------ #
class Fabrica:
    def __init__(self):
        self._contadores = {}
        self.tablas = {
            "cliente": [], "metodo_pago": [], "comercio": [], "canal_pago": [],
            "dispositivo": [], "patron": [], "cliente_dispositivo": [],
            "sesion": [], "evento": [], "transaccion": [], "devolucion": [], "alerta": [],
        }

    def siguiente_id(self, tabla):
        self._contadores[tabla] = self._contadores.get(tabla, 0) + 1
        return self._contadores[tabla]


# ------------------------------------------------------------------ #
# Generación de entidades maestras
# ------------------------------------------------------------------ #
def generar_clientes(fab, rng, fecha_inicio, fecha_fin):
    paises_por_cliente = {}
    for _ in range(N_CLIENTES):
        cid = fab.siguiente_id("cliente")
        nombre = f"{rng.choice(NOMBRES)} {rng.choice(APELLIDOS)}"
        alta = fecha_inicio - timedelta(days=int(rng.integers(0, 700)))
        fab.tablas["cliente"].append({
            "cliente_id": cid,
            "nombre_completo": nombre,
            "email": f"{nombre.lower().replace(' ', '.')}{cid}@correo.com",
            "timestamp": alta,
        })
        paises_por_cliente[cid] = rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS)
    return paises_por_cliente


def generar_comercios(fab, rng):
    categorias = ["electrónica", "moda", "alimentación", "viajes", "hogar", "ocio"]
    for _ in range(N_COMERCIOS):
        cid = fab.siguiente_id("comercio")
        fab.tablas["comercio"].append({
            "comercio_id": cid,
            "categoria_negocio": rng.choice(categorias),
            "pais": rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS),
            "nombre": f"Comercio {cid}",
            "ciudad": rng.choice(["Madrid", "Barcelona", "Valencia", "Sevilla", "Bilbao"]),
        })


def generar_dispositivos(fab, rng):
    tipos = {
        "movil": (["iPhone 14", "Samsung Galaxy S23", "Xiaomi 13"], ["iOS 17", "Android 14"]),
        "ordenador": (["MacBook Pro", "Dell XPS", "HP Pavilion"], ["macOS 14", "Windows 11", "Ubuntu 22.04"]),
        "tablet": (["iPad Air", "Samsung Tab S9"], ["iOS 17", "Android 14"]),
    }
    for _ in range(N_DISPOSITIVOS):
        did = fab.siguiente_id("dispositivo")
        tipo = rng.choice(list(tipos.keys()), p=[0.6, 0.3, 0.1])
        modelos, sistemas = tipos[tipo]
        fab.tablas["dispositivo"].append({
            "dispositivo_id": did,
            "tipo": tipo,
            "modelo": rng.choice(modelos),
            "sistema_operativo": rng.choice(sistemas),
        })


def generar_patrones(fab):
    patrones = [
        ("pico_gasto", "Pico de gasto anómalo",
         "Cantidad muy por encima de la media histórica del cliente"),
        ("card_testing", "Card testing",
         "Ráfaga de importes muy bajos en poco tiempo con el mismo método de pago"),
        ("smurfing", "Estructuración (smurfing)",
         "Varios pagos repetidos justo por debajo de un umbral redondo"),
        ("account_takeover", "Account takeover",
         "Varios intentos de login fallidos, cambio de dato y compra en la misma sesión"),
        ("ip_sospechosa", "IP/geolocalización sospechosa",
         "Sesión desde un país distinto al habitual del cliente, a menudo con VPN/proxy"),
        ("dispositivo_nuevo", "Dispositivo nuevo de alto riesgo",
         "Compra de importe alto desde un dispositivo nunca visto para ese cliente"),
        ("devolucion_abusiva", "Abuso de devoluciones",
         "Varias compras seguidas de devolución en un plazo muy corto"),
        ("bot", "Actividad tipo bot",
         "Eventos consecutivos dentro de una sesión a una velocidad no humana"),
        ("bust_out", "Bust-out",
         "Historial largo de compras pequeñas seguido de un cargo cercano al límite de crédito"),
    ]
    ids = {}
    for clave, nombre, descripcion in patrones:
        pid = fab.siguiente_id("patron")
        fab.tablas["patron"].append({
            "patron_id": pid, "nombre": nombre, "descripcion": descripcion, "condicion": clave,
        })
        ids[clave] = pid
    return ids


def generar_metodos_pago(fab, rng, clientes_ids, fecha_ref):
    por_cliente = {cid: [] for cid in clientes_ids}
    for cid in clientes_ids:
        for _ in range(int(rng.integers(1, 4))):
            mid = fab.siguiente_id("metodo_pago")
            tipo = rng.choice(["credito", "debito"], p=[0.55, 0.45])
            fab.tablas["metodo_pago"].append({
                "metodo_id": mid,
                "cliente_id": cid,
                "numero_enmascarado": f"**** **** **** {rng.integers(1000, 9999)}",
                "tipo": tipo,
                "entidad_emisora": rng.choice(["Santander", "BBVA", "CaixaBank", "Sabadell"]),
                "fecha_expiracion": (fecha_ref + timedelta(days=int(rng.integers(180, 1460)))).date(),
                "limite_credito": float(rng.integers(1000, 15000)) if tipo == "credito" else None,
                "estado": "activa",
            })
            por_cliente[cid].append(mid)
    return por_cliente


def generar_canales_pago(fab, rng, comercios_ids):
    por_comercio = {cid: [] for cid in comercios_ids}
    for cid in comercios_ids:
        for _ in range(int(rng.integers(1, 3))):
            chid = fab.siguiente_id("canal_pago")
            fab.tablas["canal_pago"].append({
                "canal_id": chid,
                "comercio_id": cid,
                "tipo": rng.choice(["web", "app", "datafono"]),
                "ubicacion": rng.choice(["online"] * 3 + ["Madrid", "Barcelona"]),
            })
            por_comercio[cid].append(chid)
    return por_comercio


def generar_cliente_dispositivo(fab, rng, clientes_ids, dispositivos_ids):
    por_cliente = {cid: [] for cid in clientes_ids}
    dispositivos_libres = list(dispositivos_ids)
    rng.shuffle(dispositivos_libres)
    puntero = 0
    for cid in clientes_ids:
        n_disp = int(rng.integers(1, 3))
        for _ in range(n_disp):
            if puntero >= len(dispositivos_libres):
                puntero = 0
            did = dispositivos_libres[puntero]
            puntero += 1
            fab.tablas["cliente_dispositivo"].append({"cliente_id": cid, "dispositivo_id": did})
            por_cliente[cid].append(did)
        # ~3% de los dispositivos se comparten con otro cliente (N:N real)
        if rng.random() < 0.03:
            otro = int(rng.choice(dispositivos_libres))
            fab.tablas["cliente_dispositivo"].append({"cliente_id": cid, "dispositivo_id": otro})
            por_cliente[cid].append(otro)
    return por_cliente


# ------------------------------------------------------------------ #
# Helpers para sesión + eventos + transacción + alerta
# ------------------------------------------------------------------ #
def ip_aleatoria(rng):
    return ".".join(str(int(rng.integers(1, 255))) for _ in range(4))


def crear_sesion(fab, rng, cliente_id, dispositivo_id, momento, pais_ip,
                  proxy_vpn=False, intentos=1, resultado="exitoso"):
    sid = fab.siguiente_id("sesion")
    fab.tablas["sesion"].append({
        "sesion_id": sid, "cliente_id": cliente_id, "dispositivo_id": dispositivo_id,
        "ip_sesion": ip_aleatoria(rng), "ip_pais": pais_ip, "proxy_vpn": bool(proxy_vpn),
        "num_intentos_login": intentos, "resultado_login": resultado, "timestamp": momento,
    })
    # eventos de login dentro de la sesión
    t = momento - timedelta(seconds=intentos * 8)
    for i in range(intentos - 1):
        fab.tablas["evento"].append({
            "evento_id": fab.siguiente_id("evento"), "sesion_id": sid, "tipo": "login_fallido",
            "timestamp": t, "detalle": "credenciales incorrectas",
        })
        t += timedelta(seconds=8)
    fab.tablas["evento"].append({
        "evento_id": fab.siguiente_id("evento"), "sesion_id": sid,
        "tipo": "login_exitoso" if resultado == "exitoso" else "login_fallido",
        "timestamp": t, "detalle": "",
    })
    return sid


def crear_evento_generico(fab, sesion_id, tipo, momento, detalle=""):
    fab.tablas["evento"].append({
        "evento_id": fab.siguiente_id("evento"), "sesion_id": sesion_id,
        "tipo": tipo, "timestamp": momento, "detalle": detalle,
    })


def crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento,
                       cantidad, estado="aprobada", tipo_operacion="compra", metodo_auth=None, moneda="EUR"):
    tid = fab.siguiente_id("transaccion")
    fab.tablas["transaccion"].append({
        "transaccion_id": tid, "metodo_id": metodo_id, "canal_id": canal_id,
        "dispositivo_id": dispositivo_id, "sesion_id": sesion_id, "timestamp": momento,
        "cantidad": round(float(cantidad), 2), "moneda": moneda, "tipo_operacion": tipo_operacion,
        "metodo_autenticacion": metodo_auth or "3D-secure", "estado": estado,
    })
    return tid


def crear_alerta(fab, rng, transaccion_id, patron_id, nota_riesgo=None):
    fab.tablas["alerta"].append({
        "alerta_id": fab.siguiente_id("alerta"), "transaccion_id": transaccion_id,
        "patron_id": patron_id, "nota_riesgo": float(nota_riesgo if nota_riesgo is not None else rng.uniform(0.6, 0.99)),
        "fecha": datetime.now(), "estado": "pendiente",
    })


def momento_aleatorio(rng, fecha_inicio, fecha_fin):
    delta = (fecha_fin - fecha_inicio).total_seconds()
    return fecha_inicio + timedelta(seconds=float(rng.uniform(0, delta)))


# ------------------------------------------------------------------ #
# Transacciones legítimas (la base "sana" del dataset)
# ------------------------------------------------------------------ #
def generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids,
                                    metodos_por_cliente, disp_por_cliente,
                                    canales_por_comercio, comercios_ids, paises_por_cliente):
    n_dias = (fecha_fin - fecha_inicio).days
    for dia in range(n_dias):
        dia_inicio = fecha_inicio + timedelta(days=dia)
        n_txn = int(rng.poisson(TRANSACCIONES_DIA_MEDIA))
        for _ in range(n_txn):
            cid = int(rng.choice(clientes_ids))
            if not metodos_por_cliente[cid] or not disp_por_cliente[cid]:
                continue
            metodo_id = int(rng.choice(metodos_por_cliente[cid]))
            dispositivo_id = int(rng.choice(disp_por_cliente[cid]))
            comercio_id = int(rng.choice(comercios_ids))
            if not canales_por_comercio[comercio_id]:
                continue
            canal_id = int(rng.choice(canales_por_comercio[comercio_id]))

            momento = dia_inicio + timedelta(seconds=int(rng.integers(0, 86400)))
            sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento,
                                      pais_ip=paises_por_cliente[cid], intentos=1, resultado="exitoso")
            crear_evento_generico(fab, sesion_id, "intento_pago", momento, "pago iniciado")

            cantidad = float(rng.lognormal(mean=3.4, sigma=0.8))
            estado = "aprobada" if rng.random() < 0.96 else "rechazada"
            crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id,
                               momento, cantidad, estado=estado)

    # devoluciones legítimas: ~2% de las transacciones aprobadas de más de 60€
    aprobadas_altas = [t for t in fab.tablas["transaccion"]
                        if t["estado"] == "aprobada" and t["cantidad"] > 60]
    n_devol = min(len(aprobadas_altas), max(1, int(len(aprobadas_altas) * 0.02)))
    indices = rng.choice(len(aprobadas_altas), size=n_devol, replace=False) if aprobadas_altas else []
    for idx in indices:
        t = aprobadas_altas[int(idx)]
        fab.tablas["devolucion"].append({
            "devolucion_id": fab.siguiente_id("devolucion"), "transaccion_id": t["transaccion_id"],
            "tipo": "reembolso_total", "motivo": "producto no conforme",
            "importe": t["cantidad"], "fecha": t["timestamp"] + timedelta(days=int(rng.integers(2, 14))),
            "estado": "procesada",
        })


# ------------------------------------------------------------------ #
# Casos de fraude — cada función crea 1+ transacciones y su(s) alerta(s)
# ------------------------------------------------------------------ #
def caso_pico_gasto(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(1500, 6000))  # muy por encima de lo habitual
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id)


def caso_card_testing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(5, 12))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = float(rng.uniform(0.5, 2.0))
        estado = "rechazada" if rng.random() < 0.8 else "aprobada"
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad, estado=estado)
        crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.8)
        t += timedelta(seconds=int(rng.integers(5, 40)))


def caso_smurfing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id, umbral=1000):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(3, 7))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = umbral - float(rng.uniform(5, 40))
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad)
        crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.75)
        t += timedelta(minutes=int(rng.integers(10, 90)))


def caso_account_takeover(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_ataque, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais_ataque,
                              proxy_vpn=True, intentos=int(rng.integers(4, 8)), resultado="exitoso")
    t = momento + timedelta(minutes=1)
    crear_evento_generico(fab, sesion_id, "cambio_dato", t, "cambio de contraseña")
    t += timedelta(minutes=2)
    crear_evento_generico(fab, sesion_id, "intento_pago", t)
    cantidad = float(rng.uniform(300, 2500))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad)
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.9)


def caso_ip_sospechosa(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_raro, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais_raro,
                              proxy_vpn=True, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(50, 800))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.7)


def caso_dispositivo_nuevo(fab, rng, cid, metodo_id, dispositivo_id_nuevo, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id_nuevo, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(400, 2000))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id_nuevo, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.65)


def caso_devolucion_abusiva(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    canal_id = int(rng.choice(canales_ids))
    cantidad = float(rng.uniform(80, 400))
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    fab.tablas["devolucion"].append({
        "devolucion_id": fab.siguiente_id("devolucion"), "transaccion_id": tid,
        "tipo": "fraudulenta", "motivo": "producto no recibido (reclamación repetida)",
        "importe": cantidad, "fecha": momento + timedelta(hours=int(rng.integers(6, 48))),
        "estado": "procesada",
    })
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.7)


def caso_bot(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for tipo in ["intento_pago", "cambio_dato", "intento_pago", "intento_pago"]:
        crear_evento_generico(fab, sesion_id, tipo, t, "generado en ráfaga")
        t += timedelta(seconds=1)  # velocidad no humana (misma franja de segundo)
    cantidad = float(rng.uniform(20, 300))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.6)


def caso_bust_out(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, fecha_inicio, momento, pais,
                   limite_credito, patron_id):
    # historial largo de compras pequeñas antes del cargo grande
    t = fecha_inicio
    while t < momento - timedelta(days=5):
        sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, t, pais_ip=pais, intentos=1)
        canal_id = int(rng.choice(canales_ids))
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t,
                           float(rng.uniform(10, 50)))
        t += timedelta(days=int(rng.integers(3, 10)))
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    canal_id = int(rng.choice(canales_ids))
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(limite_credito) * rng.uniform(0.85, 0.98)
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, nota_riesgo=0.85)


# ------------------------------------------------------------------ #
# Orquestación del fraude
# ------------------------------------------------------------------ #
def generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids):
    n_txn_total_estimado = TRANSACCIONES_DIA_MEDIA * (fecha_fin - fecha_inicio).days
    n_fraude_objetivo = int(n_txn_total_estimado * TASA_FRAUDE_OBJETIVO / (1 - TASA_FRAUDE_OBJETIVO))

    todos_los_dispositivos = set(d["dispositivo_id"] for d in fab.tablas["dispositivo"])

    for tipo, proporcion in PROPORCION_FRAUDE.items():
        n_txns_tipo = max(1, int(n_fraude_objetivo * proporcion))
        n_casos = max(1, n_txns_tipo // TXNS_POR_CASO[tipo])
        patron_id = patron_ids[tipo]

        for _ in range(n_casos):
            cid = int(rng.choice(clientes_ids))
            if not metodos_por_cliente[cid] or not disp_por_cliente[cid]:
                continue
            metodo_id = int(rng.choice(metodos_por_cliente[cid]))
            dispositivo_id = int(rng.choice(disp_por_cliente[cid]))
            comercio_id = int(rng.choice(comercios_ids))
            if not canales_por_comercio[comercio_id]:
                continue
            canal_id = int(rng.choice(canales_por_comercio[comercio_id]))
            canales_ids = canales_por_comercio[comercio_id]
            momento = momento_aleatorio(rng, fecha_inicio + timedelta(days=15), fecha_fin)
            pais_habitual = paises_por_cliente[cid]

            if tipo == "pico_gasto":
                caso_pico_gasto(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_habitual, patron_id)
            elif tipo == "card_testing":
                caso_card_testing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais_habitual, patron_id)
            elif tipo == "smurfing":
                caso_smurfing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais_habitual, patron_id)
            elif tipo == "account_takeover":
                pais_ataque = str(rng.choice(PAISES_RAROS))
                caso_account_takeover(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_ataque, patron_id)
            elif tipo == "ip_sospechosa":
                pais_raro = str(rng.choice(PAISES_RAROS))
                caso_ip_sospechosa(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_raro, patron_id)
            elif tipo == "dispositivo_nuevo":
                candidatos = list(todos_los_dispositivos - set(disp_por_cliente[cid]))
                if not candidatos:
                    continue
                disp_nuevo = int(rng.choice(candidatos))
                caso_dispositivo_nuevo(fab, rng, cid, metodo_id, disp_nuevo, canal_id, momento, pais_habitual, patron_id)
            elif tipo == "devolucion_abusiva":
                caso_devolucion_abusiva(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais_habitual, patron_id)
            elif tipo == "bot":
                caso_bot(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_habitual, patron_id)
            elif tipo == "bust_out":
                metodo = next(m for m in fab.tablas["metodo_pago"] if m["metodo_id"] == metodo_id)
                limite = metodo["limite_credito"] or 3000.0
                caso_bust_out(fab, rng, cid, metodo_id, dispositivo_id, canales_ids,
                               fecha_inicio, momento, pais_habitual, limite, patron_id)


# ------------------------------------------------------------------ #
# Carga en ClickHouse
# ------------------------------------------------------------------ #
ORDEN_INSERCION = ["cliente", "comercio", "dispositivo", "patron", "metodo_pago",
                    "canal_pago", "cliente_dispositivo", "sesion", "evento",
                    "transaccion", "devolucion", "alerta"]


def cargar_en_clickhouse(fab):
    import clickhouse_connect  # se importa aquí para que --dry-run no lo necesite instalado
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    for tabla in ORDEN_INSERCION:
        filas = fab.tablas[tabla]
        if not filas:
            continue
        df = pd.DataFrame(filas)
        client.insert_df(tabla, df)
        print(f"  {tabla}: {len(df):,} filas cargadas")


def resumen(fab):
    print("\nResumen del dataset generado:")
    for tabla in ORDEN_INSERCION:
        print(f"  {tabla}: {len(fab.tablas[tabla]):,} filas")
    n_fraude = len(fab.tablas["alerta"])
    n_txn = len(fab.tablas["transaccion"])
    print(f"\n  Transacciones totales: {n_txn:,}")
    print(f"  Transacciones marcadas como fraude (vía alerta): {n_fraude:,} "
          f"({n_fraude / n_txn * 100:.2f}%)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No conecta a ClickHouse, solo genera y resume")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dias", type=int, default=180)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)

    fecha_fin = datetime.now()
    fecha_inicio = fecha_fin - timedelta(days=args.dias)

    fab = Fabrica()
    fab.tablas = {k: [] for k in fab.tablas}  # reset limpio

    print("Generando entidades maestras...")
    paises_por_cliente = generar_clientes(fab, rng, fecha_inicio, fecha_fin)
    generar_comercios(fab, rng)
    generar_dispositivos(fab, rng)
    patron_ids = generar_patrones(fab)

    clientes_ids = [c["cliente_id"] for c in fab.tablas["cliente"]]
    comercios_ids = [c["comercio_id"] for c in fab.tablas["comercio"]]
    dispositivos_ids = [d["dispositivo_id"] for d in fab.tablas["dispositivo"]]

    metodos_por_cliente = generar_metodos_pago(fab, rng, clientes_ids, fecha_inicio)
    canales_por_comercio = generar_canales_pago(fab, rng, comercios_ids)
    disp_por_cliente = generar_cliente_dispositivo(fab, rng, clientes_ids, dispositivos_ids)

    print("Generando transacciones legítimas (esto puede tardar un poco)...")
    generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids,
                                    metodos_por_cliente, disp_por_cliente,
                                    canales_por_comercio, comercios_ids, paises_por_cliente)

    print("Inyectando casos de fraude...")
    generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids)

    resumen(fab)

    if args.dry_run:
        print("\n--dry-run: no se ha insertado nada en ClickHouse.")
    else:
        print("\nCargando en ClickHouse...")
        cargar_en_clickhouse(fab)
        print("Listo.")


if __name__ == "__main__":
    main()