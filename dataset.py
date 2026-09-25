import argparse
import random
from datetime import datetime, timedelta, date

import numpy as np
import pandas as pd
from faker import Faker

fake = Faker("es_ES")
HOST = "localhost"
PORT = 8123
USER = "default"
PASSWORD = "password"
DATABASE = "fraude_pagos"

N_CLIENTES = 2000
N_COMERCIOS = 30
N_DISPOSITIVOS = 2600
TRANSACCIONES_DIA_MEDIA = 200     
TASA_FRAUDE_OBJETIVO = 0.02       

TIPOS_FRAUDE = ["pico_gasto", "card_testing", "smurfing", "account_takeover", "ip_sospechosa",
                "dispositivo_nuevo", "devolucion_abusiva", "bot", "bust_out"]


def calcular_proporciones_fraude(modo, rng):
    if modo == "igual":
        p = 1.0 / len(TIPOS_FRAUDE)
        return {tipo: p for tipo in TIPOS_FRAUDE}
    if modo == "aleatoria":
        valores = rng.dirichlet(np.ones(len(TIPOS_FRAUDE)))
        return dict(zip(TIPOS_FRAUDE, valores))
    raise ValueError(f"modo de proporciones desconocido: {modo}")

TXNS_POR_CASO = {
    "pico_gasto": 1, "card_testing": 8, "smurfing": 5, "account_takeover": 1,
    "ip_sospechosa": 1, "dispositivo_nuevo": 1, "devolucion_abusiva": 1,
    "bot": 1, "bust_out": 1,
}

PAISES_HABITUALES = ["España", "Francia", "Alemania", "Portugal", "Italia",
                      "Reino Unido", "Países Bajos", "Bélgica", "Irlanda", "Polonia"]
PAISES_HABITUALES_PESOS = [0.50, 0.10, 0.09, 0.07, 0.07, 0.06, 0.04, 0.03, 0.02, 0.02]
PAISES_RAROS = ["Rusia", "Nigeria", "Vietnam", "Ucrania", "Indonesia", "Filipinas",
                "China", "Pakistán", "Brasil", "India", "Sudáfrica", "Egipto"]



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


def generar_clientes(fab, rng, fecha_inicio, fecha_fin):
    paises_por_cliente = {}
    for _ in range(N_CLIENTES):
        cid = fab.siguiente_id("cliente")
        nombre = fake.name()
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
    categorias = ["electrónica", "moda", "alimentación", "viajes", "hogar", "ocio",
                  "deporte", "belleza", "juguetería", "librería", "mascotas",
                  "bricolaje", "automoción", "joyería", "farmacia", "restauración"]
    for _ in range(N_COMERCIOS):
        cid = fab.siguiente_id("comercio")
        fab.tablas["comercio"].append({
            "comercio_id": cid,
            "categoria_negocio": rng.choice(categorias),
            "pais": rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS),
            "nombre": fake.company(),
            "ciudad": fake.city(),
        })


def generar_dispositivos(fab, rng):
    tipos = {
        "movil": (["iPhone 14", "Samsung Galaxy S23", "Xiaomi 13"], ["iOS 17", "Android 14"]),
        "ordenador": (["MacBook Pro", "Dell XPS", "HP Pavilion"], ["macOS 14", "Windows 11", "Ubuntu 22.04"]),
        "tablet": (["iPad Air", "Samsung Tab S9"], ["iOS 17", "Android 14"]),
        "datafono": (["Ingenico Move 5000", "Verifone V400m", "PAX A920"], ["Android POS", "Linux embebido"]),
    }
    for _ in range(N_DISPOSITIVOS):
        did = fab.siguiente_id("dispositivo")
        tipo = rng.choice(list(tipos.keys()), p=[0.55, 0.25, 0.10, 0.10])
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
         "Cantidad muy por encima de la media histórica del cliente",
         "importe de varios miles de euros, muy por encima del gasto habitual del cliente"),
        ("card_testing", "Card testing",
         "Ráfaga de importes muy bajos en poco tiempo con el mismo método de pago",
         ">=5 transacciones del mismo metodo_id, importe <2€, en menos de 10 minutos"),
        ("smurfing", "Estructuración (smurfing)",
         "Varios pagos repetidos justo por debajo de un umbral redondo",
         ">=3 pagos del mismo metodo_id, cada uno justo por debajo de 1000€, repartidos en varias horas"),
        ("account_takeover", "Account takeover",
         "Varios intentos de login fallidos, cambio de dato y compra en la misma sesión",
         "evento cambio_dato seguido de una transaccion >300€ en menos de 1h, misma sesion, con >=4 intentos de login previos"),
        ("ip_sospechosa", "IP/geolocalización sospechosa",
         "Sesión desde un país distinto al habitual del cliente, a menudo con VPN/proxy",
         "sesion con proxy_vpn=true e ip_pais distinto al pais habitual del cliente"),
        ("dispositivo_nuevo", "Dispositivo nuevo de alto riesgo",
         "Compra de importe alto desde un dispositivo nunca visto para ese cliente",
         "transaccion >400€ desde un dispositivo_id no presente en cliente_dispositivo para ese cliente"),
        ("devolucion_abusiva", "Abuso de devoluciones",
         "Varias compras seguidas de devolución en un plazo muy corto",
         "devolucion tipo=fraudulenta registrada en menos de 48h tras la transaccion"),
        ("bot", "Actividad tipo bot",
         "Eventos consecutivos dentro de una sesión a una velocidad no humana",
         ">=4 eventos en la misma sesion separados por <1 segundo entre si"),
        ("bust_out", "Bust-out",
         "Historial largo de compras pequeñas seguido de un cargo cercano al límite de crédito",
         "historial de compras pequeñas durante semanas seguido de un cargo entre el 85% y 98% del limite_credito"),
    ]
    ids = {}
    for clave, nombre, descripcion, condicion in patrones:
        pid = fab.siguiente_id("patron")
        fab.tablas["patron"].append({
            "patron_id": pid, "nombre": nombre, "descripcion": descripcion, "condicion": condicion,
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


def generar_canales_pago(fab, rng, comercios_ids, dispositivos_datafono_ids):
    por_comercio = {cid: [] for cid in comercios_ids}
    canal_tipo = {}
    canal_a_datafono = {}
    for cid in comercios_ids:
        for _ in range(int(rng.integers(1, 3))):
            chid = fab.siguiente_id("canal_pago")
            tipo = rng.choice(["web", "app", "datafono"])
            fab.tablas["canal_pago"].append({
                "canal_id": chid,
                "comercio_id": cid,
                "tipo": tipo,
                "ubicacion": rng.choice(["online"] * 3 + ["Madrid", "Barcelona"]),
            })
            por_comercio[cid].append(chid)
            canal_tipo[chid] = tipo
            if tipo == "datafono" and dispositivos_datafono_ids:
                canal_a_datafono[chid] = int(rng.choice(dispositivos_datafono_ids))
    return por_comercio, canal_tipo, canal_a_datafono


def generar_cliente_dispositivo(fab, rng, clientes_ids, dispositivos_personales_ids):
    por_cliente = {cid: [] for cid in clientes_ids}
    dispositivos_libres = list(dispositivos_personales_ids)
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
        if rng.random() < 0.03:
            otro = int(rng.choice(dispositivos_libres))
            fab.tablas["cliente_dispositivo"].append({"cliente_id": cid, "dispositivo_id": otro})
            por_cliente[cid].append(otro)
    return por_cliente

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


def crear_alerta(fab, rng, transaccion_id, patron_id, momento, nota_riesgo=None):
    fecha_deteccion = momento + timedelta(seconds=int(rng.integers(2, 180)))
    fab.tablas["alerta"].append({
        "alerta_id": fab.siguiente_id("alerta"), "transaccion_id": transaccion_id,
        "patron_id": patron_id, "nota_riesgo": float(nota_riesgo if nota_riesgo is not None else rng.uniform(0.6, 0.99)),
        "fecha": fecha_deteccion, "estado": "pendiente",
    })


def momento_aleatorio(rng, fecha_inicio, fecha_fin):
    delta = (fecha_fin - fecha_inicio).total_seconds()
    return fecha_inicio + timedelta(seconds=float(rng.uniform(0, delta)))

def generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids,
                                    metodos_por_cliente, disp_por_cliente,
                                    canales_por_comercio, comercios_ids, paises_por_cliente,
                                    canal_tipo, canal_a_datafono):
    n_dias = (fecha_fin - fecha_inicio).days
    for dia in range(n_dias):
        dia_inicio = fecha_inicio + timedelta(days=dia)
        n_txn = int(rng.poisson(TRANSACCIONES_DIA_MEDIA))
        for _ in range(n_txn):
            cid = int(rng.choice(clientes_ids))
            if not metodos_por_cliente[cid] or not disp_por_cliente[cid]:
                continue
            metodo_id = int(rng.choice(metodos_por_cliente[cid]))
            comercio_id = int(rng.choice(comercios_ids))
            if not canales_por_comercio[comercio_id]:
                continue
            canal_id = int(rng.choice(canales_por_comercio[comercio_id]))

            if canal_tipo[canal_id] == "datafono" and canal_id in canal_a_datafono:
                dispositivo_id = canal_a_datafono[canal_id]
            else:
                dispositivo_id = int(rng.choice(disp_por_cliente[cid]))

            momento = dia_inicio + timedelta(seconds=int(rng.integers(0, 86400)))
            sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento,
                                      pais_ip=paises_por_cliente[cid], intentos=1, resultado="exitoso")
            crear_evento_generico(fab, sesion_id, "intento_pago", momento, "pago iniciado")

            cantidad = float(rng.lognormal(mean=3.4, sigma=0.8))
            estado = "aprobada" if rng.random() < 0.96 else "rechazada"
            crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id,
                               momento, cantidad, estado=estado)

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

def caso_pico_gasto(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(1500, 6000))  
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, momento)


def caso_card_testing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(5, 12))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = float(rng.uniform(0.5, 2.0))
        estado = "rechazada" if rng.random() < 0.8 else "aprobada"
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad, estado=estado)
        crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.8)
        t += timedelta(seconds=int(rng.integers(5, 40)))


def caso_smurfing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id, umbral=1000):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(3, 7))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = umbral - float(rng.uniform(5, 40))
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad)
        crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.75)
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
    crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.9)


def caso_ip_sospechosa(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_raro, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais_raro,
                              proxy_vpn=True, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(50, 800))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.7)


def caso_dispositivo_nuevo(fab, rng, cid, metodo_id, dispositivo_id_nuevo, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id_nuevo, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(400, 2000))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id_nuevo, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.65)


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
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.7)


def caso_bot(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for tipo in ["intento_pago", "cambio_dato", "intento_pago", "intento_pago"]:
        crear_evento_generico(fab, sesion_id, tipo, t, "generado en ráfaga")
        t += timedelta(seconds=1) 
    cantidad = float(rng.uniform(20, 300))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.6)


def caso_bust_out(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, fecha_inicio, momento, pais,
                   limite_credito, patron_id):
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
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.85)


def generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids, proporcion_fraude):
    n_txn_total_estimado = TRANSACCIONES_DIA_MEDIA * (fecha_fin - fecha_inicio).days
    n_fraude_objetivo = int(n_txn_total_estimado * TASA_FRAUDE_OBJETIVO / (1 - TASA_FRAUDE_OBJETIVO))

    todos_los_dispositivos = set(d["dispositivo_id"] for d in fab.tablas["dispositivo"])

    for tipo, proporcion in proporcion_fraude.items():
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


ORDEN_INSERCION = ["cliente", "comercio", "dispositivo", "patron", "metodo_pago",
                    "canal_pago", "cliente_dispositivo", "sesion", "evento",
                    "transaccion", "devolucion", "alerta"]


def cargar_en_clickhouse(fab):
    import clickhouse_connect  
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
    parser.add_argument("--proporciones", choices=["igual", "aleatoria"], default="aleatoria",
                         help="como repartir el fraude entre los 9 tipos: aleatoria (por defecto) o igual")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)
    Faker.seed(args.seed)

    fecha_fin = datetime.now()
    fecha_inicio = fecha_fin - timedelta(days=args.dias)

    fab = Fabrica()
    fab.tablas = {k: [] for k in fab.tablas} 

    print("Generando entidades maestras...")
    paises_por_cliente = generar_clientes(fab, rng, fecha_inicio, fecha_fin)
    generar_comercios(fab, rng)
    generar_dispositivos(fab, rng)
    patron_ids = generar_patrones(fab)

    clientes_ids = [c["cliente_id"] for c in fab.tablas["cliente"]]
    comercios_ids = [c["comercio_id"] for c in fab.tablas["comercio"]]
    dispositivos_personales_ids = [d["dispositivo_id"] for d in fab.tablas["dispositivo"] if d["tipo"] != "datafono"]
    dispositivos_datafono_ids = [d["dispositivo_id"] for d in fab.tablas["dispositivo"] if d["tipo"] == "datafono"]

    metodos_por_cliente = generar_metodos_pago(fab, rng, clientes_ids, fecha_inicio)
    canales_por_comercio, canal_tipo, canal_a_datafono = generar_canales_pago(
        fab, rng, comercios_ids, dispositivos_datafono_ids
    )
    disp_por_cliente = generar_cliente_dispositivo(fab, rng, clientes_ids, dispositivos_personales_ids)

    print("Generando transacciones legítimas (esto puede tardar un poco)...")
    generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids,
                                    metodos_por_cliente, disp_por_cliente,
                                    canales_por_comercio, comercios_ids, paises_por_cliente,
                                    canal_tipo, canal_a_datafono)

    proporcion_fraude = calcular_proporciones_fraude(args.proporciones, rng)
    print(f"Inyectando casos de fraude (reparto: {args.proporciones})...")
    for tipo, p in proporcion_fraude.items():
        print(f"    {tipo}: {p:.3f}")
    generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids, proporcion_fraude)

    resumen(fab)

    if args.dry_run:
        print("\n--dry-run: no se ha insertado nada en ClickHouse.")
    else:
        print("\nCargando en ClickHouse...")
        cargar_en_clickhouse(fab)
        print("Listo.")


if __name__ == "__main__":
    main()