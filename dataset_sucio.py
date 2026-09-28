import argparse
import random
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

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

# ---------------------------------------------------------------------------
# Perfiles de cliente: cada cliente gasta y compra con una intensidad distinta
# ---------------------------------------------------------------------------
GASTO_LOG_MEDIA = 3.4                  # log-importe típico del conjunto de clientes (~30€)
GASTO_LOG_DISPERSION_CLIENTES = 0.6    # cuánto varía el gasto típico ENTRE clientes
GASTO_LOG_SIGMA_CLIENTE = 0.6          # variabilidad de importes DENTRO de un mismo cliente
ACTIVIDAD_FORMA = 1.5                  # forma de la gamma de actividad (más baja = más desigual)

# ---------------------------------------------------------------------------
# Ruido legítimo: comportamientos normales que "parecen" sospechosos.
# Sin esto el fraude y lo normal quedan perfectamente separados.
# ---------------------------------------------------------------------------
PROB_VPN_LEGITIMO = 0.03              # sesiones online normales con VPN/proxy
PROB_VIAJE = 0.02                     # sesiones normales desde otro país (viajes)
PROB_VIAJE_PAIS_RARO = 0.15           # de esos viajes, % a un país de PAISES_RAROS
PROB_ERROR_LOGIN = 0.06               # sesiones online normales con algún intento de login fallido
PROB_COMPRA_GRANDE = 0.005            # compras normales de importe alto (viajes, electrónica...)
PROB_REINTENTO_PAGO = 0.01            # pago rechazado que se reintenta 1-3 veces en < 1 min
PROB_COMPRAS_SEGUIDAS = 0.0005        # 3-7 compras pequeñas seguidas (in-app, máquinas...)
PROB_DISPOSITIVO_AJENO = 0.01         # compra desde un dispositivo que no es del cliente (trabajo, familiar)
PROB_CLIENTE_ESTRENA_DISPOSITIVO = 0.05  # clientes que estrenan móvil/ordenador a mitad del periodo
PROB_DEVOLUCION_RAPIDA_LEGITIMA = 0.20   # devoluciones normales registradas en < 48h
FACTOR_FIN_DE_SEMANA = 1.2            # sábado y domingo tienen un 20% más de actividad que un día laborable
PESOS_HORA = np.array([1.0, 0.6, 0.4, 0.3, 0.3, 0.4, 0.8, 1.5, 2.5, 3.0, 3.2, 3.5,
                       4.0, 4.0, 3.5, 3.2, 3.2, 3.5, 4.0, 4.5, 4.5, 4.0, 3.0, 2.0])
PESOS_HORA = PESOS_HORA / PESOS_HORA.sum()   # actividad normal por hora del día (el fraude no sigue este patrón)

# ---------------------------------------------------------------------------
# Fraude menos "de libro": no todos los casos llevan todas las señales
# ---------------------------------------------------------------------------
PROB_VPN_ACCOUNT_TAKEOVER = 0.6       # antes 100%
PROB_PAIS_RARO_ACCOUNT_TAKEOVER = 0.7 # el resto, desde el país habitual del cliente
PROB_VPN_IP_SOSPECHOSA = 0.7          # antes 100%
PROB_PAIS_RARO_IP_SOSPECHOSA = 0.8    # el resto, desde otro país "habitual" distinto al del cliente

# ---------------------------------------------------------------------------
# Suciedad de datos (solo con --ensuciar): para probar el informe de limpieza
# ---------------------------------------------------------------------------
PROB_SUCIEDAD = 0.01

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
    """Crea los clientes con su país habitual y su perfil de gasto/actividad propio."""
    paises_por_cliente = {}
    perfiles = {}
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
        paises_por_cliente[cid] = str(rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS))
        perfiles[cid] = {
            # gasto típico propio: unos clientes gastan ~10€ por compra y otros ~100€
            "log_gasto": float(rng.normal(GASTO_LOG_MEDIA, GASTO_LOG_DISPERSION_CLIENTES)),
            # actividad propia: unos compran casi a diario y otros un par de veces al mes
            "actividad": float(rng.gamma(ACTIVIDAD_FORMA)),
        }
    return paises_por_cliente, perfiles


def generar_comercios(fab, rng):
    categorias = ["electrónica", "moda", "alimentación", "viajes", "hogar", "ocio",
                  "deporte", "belleza", "juguetería", "librería", "mascotas",
                  "bricolaje", "automoción", "joyería", "farmacia", "restauración"]
    for _ in range(N_COMERCIOS):
        cid = fab.siguiente_id("comercio")
        fab.tablas["comercio"].append({
            "comercio_id": cid,
            "categoria_negocio": str(rng.choice(categorias)),
            "pais": str(rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS)),
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
        tipo = str(rng.choice(list(tipos.keys()), p=[0.55, 0.25, 0.10, 0.10]))
        modelos, sistemas = tipos[tipo]
        fab.tablas["dispositivo"].append({
            "dispositivo_id": did,
            "tipo": tipo,
            "modelo": str(rng.choice(modelos)),
            "sistema_operativo": str(rng.choice(sistemas)),
        })


def generar_patrones(fab):
    patrones = [
        ("pico_gasto", "Pico de gasto anómalo",
         "Cantidad muy por encima del gasto habitual de ese cliente",
         "importe entre 6 y 30 veces el gasto habitual (mediana) del propio cliente"),
        ("card_testing", "Card testing",
         "Ráfaga de importes muy bajos en poco tiempo con el mismo método de pago",
         ">=5 transacciones del mismo metodo_id, importe <5€, separadas por segundos o pocos minutos"),
        ("smurfing", "Estructuración (smurfing)",
         "Varios pagos repetidos justo por debajo de un umbral redondo",
         ">=3 pagos del mismo metodo_id, cada uno justo por debajo de 1000€, repartidos en varias horas"),
        ("account_takeover", "Account takeover",
         "Varios intentos de login fallidos, cambio de dato y compra en la misma sesión",
         "evento cambio_dato seguido de una transaccion >300€ en menos de 1h, misma sesion, con >=4 intentos de login previos (a menudo con VPN/proxy)"),
        ("ip_sospechosa", "IP/geolocalización sospechosa",
         "Sesión desde un país distinto al habitual del cliente, a menudo con VPN/proxy",
         "sesion con ip_pais distinto al pais habitual del cliente, normalmente con proxy_vpn=true"),
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
            tipo = str(rng.choice(["credito", "debito"], p=[0.55, 0.45]))
            fab.tablas["metodo_pago"].append({
                "metodo_id": mid,
                "cliente_id": cid,
                "numero_enmascarado": f"**** **** **** {rng.integers(1000, 9999)}",
                "tipo": tipo,
                "entidad_emisora": str(rng.choice(["Santander", "BBVA", "CaixaBank", "Sabadell"])),
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
            tipo = str(rng.choice(["web", "app", "datafono"]))
            fab.tablas["canal_pago"].append({
                "canal_id": chid,
                "comercio_id": cid,
                "tipo": tipo,
                "ubicacion": str(rng.choice(["online"] * 3 + ["Madrid", "Barcelona"])),
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


def generar_estrenos_dispositivo(fab, rng, clientes_ids, dispositivos_personales_ids, fecha_inicio, fecha_fin):
    """Algunos clientes estrenan un dispositivo a mitad del periodo (móvil nuevo...).
    Queda registrado en cliente_dispositivo, pero antes de la fecha de estreno no se usa nunca:
    la primera compra con él es un 'dispositivo nuevo' totalmente legítimo."""
    registrados = defaultdict(set)
    for fila in fab.tablas["cliente_dispositivo"]:
        registrados[fila["cliente_id"]].add(fila["dispositivo_id"])
    estrenos = {}
    dias = (fecha_fin - fecha_inicio).days
    for cid in clientes_ids:
        if rng.random() < PROB_CLIENTE_ESTRENA_DISPOSITIVO:
            did = int(rng.choice(dispositivos_personales_ids))
            if did in registrados[cid]:
                continue
            fecha = fecha_inicio + timedelta(days=int(rng.integers(20, max(21, dias - 10))))
            fab.tablas["cliente_dispositivo"].append({"cliente_id": cid, "dispositivo_id": did})
            estrenos[cid] = (did, fecha)
    return estrenos


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


# Incluye el parámetro es_fraude (0 por defecto, 1 para transacciones fraudulentas)
def crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento,
                       cantidad, estado="aprobada", tipo_operacion="compra", metodo_auth=None, moneda="EUR", es_fraude=0):
    tid = fab.siguiente_id("transaccion")
    fab.tablas["transaccion"].append({
        "transaccion_id": tid, "metodo_id": metodo_id, "canal_id": canal_id,
        "dispositivo_id": dispositivo_id, "sesion_id": sesion_id, "timestamp": momento,
        "cantidad": round(float(cantidad), 2), "moneda": moneda, "tipo_operacion": tipo_operacion,
        "metodo_autenticacion": metodo_auth or "3D-secure", "estado": estado,
        "es_fraude": int(es_fraude),
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


def momento_en_dia(rng, dia_inicio):
    """Hora del día según el patrón de actividad normal (poca actividad de madrugada)."""
    hora = int(rng.choice(24, p=PESOS_HORA))
    return dia_inicio + timedelta(hours=hora, seconds=int(rng.integers(0, 3600)))


def generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles,
                                    metodos_por_cliente, disp_por_cliente, estrenos,
                                    canales_por_comercio, comercios_ids, paises_por_cliente,
                                    canal_tipo, canal_a_datafono, dispositivos_personales_ids):
    pesos_clientes = np.array([perfiles[c]["actividad"] for c in clientes_ids])
    pesos_clientes = pesos_clientes / pesos_clientes.sum()
    n_dias = (fecha_fin - fecha_inicio).days

    # factores normalizados para que la media semanal siga siendo TRANSACCIONES_DIA_MEDIA
    factor_laborable = 7 / (5 + 2 * FACTOR_FIN_DE_SEMANA)
    factor_finde = factor_laborable * FACTOR_FIN_DE_SEMANA

    for dia in range(n_dias):
        dia_inicio = fecha_inicio + timedelta(days=dia)
        factor = factor_finde if dia_inicio.weekday() >= 5 else factor_laborable
        n_txn = int(rng.poisson(TRANSACCIONES_DIA_MEDIA * factor))
        clientes_del_dia = rng.choice(clientes_ids, size=n_txn, p=pesos_clientes)

        for cid in clientes_del_dia:
            cid = int(cid)
            if not metodos_por_cliente[cid] or not disp_por_cliente[cid]:
                continue
            metodo_id = int(rng.choice(metodos_por_cliente[cid]))
            comercio_id = int(rng.choice(comercios_ids))
            if not canales_por_comercio[comercio_id]:
                continue
            canal_id = int(rng.choice(canales_por_comercio[comercio_id]))
            es_online = canal_tipo[canal_id] != "datafono"
            momento = momento_en_dia(rng, dia_inicio)

            # --- dispositivo ---
            if not es_online and canal_id in canal_a_datafono:
                dispositivo_id = canal_a_datafono[canal_id]
            elif rng.random() < PROB_DISPOSITIVO_AJENO:
                # ordenador del trabajo, móvil de un familiar... (no está en cliente_dispositivo)
                dispositivo_id = int(rng.choice(dispositivos_personales_ids))
            elif cid in estrenos and momento >= estrenos[cid][1] and rng.random() < 0.7:
                dispositivo_id = estrenos[cid][0]
            else:
                dispositivo_id = int(rng.choice(disp_por_cliente[cid]))

            # --- país, VPN y login ---
            pais = paises_por_cliente[cid]
            if rng.random() < PROB_VIAJE:
                if rng.random() < PROB_VIAJE_PAIS_RARO:
                    pais = str(rng.choice(PAISES_RAROS))
                else:
                    pais = str(rng.choice([p for p in PAISES_HABITUALES if p != pais]))
            vpn = es_online and rng.random() < PROB_VPN_LEGITIMO
            intentos = 1
            if es_online and rng.random() < PROB_ERROR_LOGIN:
                intentos = int(rng.choice([2, 3, 4, 5], p=[0.70, 0.20, 0.07, 0.03]))

            sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais,
                                      proxy_vpn=vpn, intentos=intentos, resultado="exitoso")
            crear_evento_generico(fab, sesion_id, "intento_pago", momento, "pago iniciado")

            # --- importe según el perfil del cliente ---
            if rng.random() < PROB_COMPRA_GRANDE:
                cantidad = float(rng.uniform(500, 3000))
            else:
                cantidad = float(rng.lognormal(mean=perfiles[cid]["log_gasto"], sigma=GASTO_LOG_SIGMA_CLIENTE))

            r = rng.random()
            if r < PROB_REINTENTO_PAGO:
                # pago rechazado y reintentado varias veces en pocos segundos (ráfaga legítima corta)
                t = momento
                n_reintentos = int(rng.integers(1, 4))
                for k in range(n_reintentos + 1):
                    ultimo = k == n_reintentos
                    estado = "aprobada" if ultimo and rng.random() < 0.8 else "rechazada"
                    if k > 0:
                        crear_evento_generico(fab, sesion_id, "intento_pago", t, "reintento de pago")
                    crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id,
                                       t, cantidad, estado=estado, es_fraude=0)
                    t += timedelta(seconds=int(rng.integers(10, 45)))
            elif r < PROB_REINTENTO_PAGO + PROB_COMPRAS_SEGUIDAS:
                # varias compras pequeñas seguidas (in-app, máquinas...): parecido al card testing
                t = momento
                for k in range(int(rng.integers(3, 8))):
                    if k > 0:
                        crear_evento_generico(fab, sesion_id, "intento_pago", t, "compra seguida")
                    crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id,
                                       t, float(rng.uniform(2, 15)), es_fraude=0)
                    t += timedelta(seconds=int(rng.integers(10, 60)))
            else:
                estado = "aprobada" if rng.random() < 0.96 else "rechazada"
                crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id,
                                   momento, cantidad, estado=estado, es_fraude=0)

    # --- devoluciones legítimas (~2% de las compras aprobadas de más de 60€) ---
    aprobadas_altas = [t for t in fab.tablas["transaccion"]
                        if t["estado"] == "aprobada" and t["cantidad"] > 60]
    n_devol = min(len(aprobadas_altas), max(1, int(len(aprobadas_altas) * 0.02)))
    indices = rng.choice(len(aprobadas_altas), size=n_devol, replace=False) if aprobadas_altas else []
    for idx in indices:
        t = aprobadas_altas[int(idx)]
        if rng.random() < PROB_DEVOLUCION_RAPIDA_LEGITIMA:
            fecha = t["timestamp"] + timedelta(hours=int(rng.integers(12, 48)))
        else:
            fecha = t["timestamp"] + timedelta(days=int(rng.integers(2, 14)))
        fab.tablas["devolucion"].append({
            "devolucion_id": fab.siguiente_id("devolucion"), "transaccion_id": t["transaccion_id"],
            "tipo": "reembolso_total", "motivo": "producto no conforme",
            "importe": t["cantidad"], "fecha": fecha, "estado": "procesada",
        })


def caso_pico_gasto(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais, patron_id, log_gasto_cliente):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    # relativo al gasto habitual del cliente: para uno que gasta 10€ un pico son 60-300€
    cantidad = float(np.exp(log_gasto_cliente) * rng.uniform(6, 30))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, momento)


def caso_card_testing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(5, 12))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = float(rng.uniform(0.5, 5.0))
        estado = "rechazada" if rng.random() < 0.8 else "aprobada"
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad, estado=estado, es_fraude=1)
        crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.8)
        t += timedelta(seconds=int(rng.integers(5, 75)))


def caso_smurfing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id, umbral=1000):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    t = momento
    for _ in range(int(rng.integers(3, 7))):
        canal_id = int(rng.choice(canales_ids))
        cantidad = umbral - float(rng.uniform(5, 40))
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad, es_fraude=1)
        crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.75)
        t += timedelta(minutes=int(rng.integers(10, 90)))


def caso_account_takeover(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_ataque, patron_id):
    vpn = rng.random() < PROB_VPN_ACCOUNT_TAKEOVER
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais_ataque,
                              proxy_vpn=vpn, intentos=int(rng.integers(4, 8)), resultado="exitoso")
    t = momento + timedelta(minutes=1)
    crear_evento_generico(fab, sesion_id, "cambio_dato", t, "cambio de contraseña")
    t += timedelta(minutes=2)
    crear_evento_generico(fab, sesion_id, "intento_pago", t)
    cantidad = float(rng.uniform(300, 2500))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, t, nota_riesgo=0.9)


def caso_ip_sospechosa(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_distinto, patron_id):
    vpn = rng.random() < PROB_VPN_IP_SOSPECHOSA
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais_distinto,
                              proxy_vpn=vpn, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(50, 800))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.7)


def caso_dispositivo_nuevo(fab, rng, cid, metodo_id, dispositivo_id_nuevo, canal_id, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id_nuevo, momento, pais_ip=pais, intentos=1)
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(rng.uniform(400, 2000))
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id_nuevo, momento, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.65)


def caso_devolucion_abusiva(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais, patron_id):
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    canal_id = int(rng.choice(canales_ids))
    cantidad = float(rng.uniform(80, 400))
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad, es_fraude=1)
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
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.6)


def caso_bust_out(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, fecha_inicio, momento, pais,
                   limite_credito, patron_id):
    t = fecha_inicio
    while t < momento - timedelta(days=5):
        sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, t, pais_ip=pais, intentos=1)
        canal_id = int(rng.choice(canales_ids))
        crear_evento_generico(fab, sesion_id, "intento_pago", t)
        # Compras históricas previas dentro del bust-out (legítimas) -> es_fraude=0
        crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t,
                           float(rng.uniform(10, 50)), es_fraude=0)
        t += timedelta(days=int(rng.integers(3, 10)))
    sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento, pais_ip=pais, intentos=1)
    canal_id = int(rng.choice(canales_ids))
    crear_evento_generico(fab, sesion_id, "intento_pago", momento)
    cantidad = float(limite_credito) * rng.uniform(0.85, 0.98)
    # Cargo final masivo fraudulento -> es_fraude=1
    tid = crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad, es_fraude=1)
    crear_alerta(fab, rng, tid, patron_id, momento, nota_riesgo=0.85)


def generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids, proporcion_fraude):
    # se calcula sobre las transacciones normales ya generadas (incluye reintentos y compras seguidas)
    n_txn_normales = len(fab.tablas["transaccion"])
    n_fraude_objetivo = int(n_txn_normales * TASA_FRAUDE_OBJETIVO / (1 - TASA_FRAUDE_OBJETIVO))

    todos_los_dispositivos = set(d["dispositivo_id"] for d in fab.tablas["dispositivo"])
    # dispositivos registrados de cada cliente (incluidos los que estrena a mitad de periodo)
    registrados = defaultdict(set)
    for fila in fab.tablas["cliente_dispositivo"]:
        registrados[fila["cliente_id"]].add(fila["dispositivo_id"])

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
                caso_pico_gasto(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_habitual,
                                patron_id, perfiles[cid]["log_gasto"])
            elif tipo == "card_testing":
                caso_card_testing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais_habitual, patron_id)
            elif tipo == "smurfing":
                caso_smurfing(fab, rng, cid, metodo_id, dispositivo_id, canales_ids, momento, pais_habitual, patron_id)
            elif tipo == "account_takeover":
                if rng.random() < PROB_PAIS_RARO_ACCOUNT_TAKEOVER:
                    pais_ataque = str(rng.choice(PAISES_RAROS))
                else:
                    pais_ataque = pais_habitual
                caso_account_takeover(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_ataque, patron_id)
            elif tipo == "ip_sospechosa":
                if rng.random() < PROB_PAIS_RARO_IP_SOSPECHOSA:
                    pais_distinto = str(rng.choice(PAISES_RAROS))
                else:
                    pais_distinto = str(rng.choice([p for p in PAISES_HABITUALES if p != pais_habitual]))
                caso_ip_sospechosa(fab, rng, cid, metodo_id, dispositivo_id, canal_id, momento, pais_distinto, patron_id)
            elif tipo == "dispositivo_nuevo":
                candidatos = list(todos_los_dispositivos - registrados[cid])
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


# ---------------------------------------------------------------------------
# Suciedad de datos (--ensuciar)
# ---------------------------------------------------------------------------
def _sin_tildes(texto):
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()


def _variante_texto(rng, texto):
    """Mismo valor escrito de otra forma: mayúsculas, minúsculas, espacios, sin tildes..."""
    opciones = [texto.lower(), texto.upper(), f" {texto} ", _sin_tildes(texto), f"{texto} "]
    opciones = [o for o in opciones if o != texto] or [f" {texto}"]
    return str(rng.choice(opciones))


def ensuciar(fab, rng, prob=PROB_SUCIEDAD):
    """Introduce problemas típicos de calidad de datos para poder probar el informe de limpieza.
    No toca claves primarias ni foráneas (salvo las filas duplicadas), así que la integridad
    referencial se mantiene. Devuelve un resumen de todo lo que se ha ensuciado."""
    resumen = {}

    def muestra(filas, p):
        if not filas:
            return []
        n = max(1, int(round(len(filas) * p)))
        idx = rng.choice(len(filas), size=min(n, len(filas)), replace=False)
        return [filas[int(i)] for i in idx]

    # 1) Campos de texto vacíos (las columnas String no admiten NULL -> cadena vacía)
    for tabla, col in [("cliente", "email"), ("comercio", "ciudad"),
                       ("dispositivo", "modelo"), ("sesion", "ip_pais")]:
        filas = muestra(fab.tablas[tabla], prob)
        for f in filas:
            f[col] = ""
        resumen[f"{tabla}.{col} vacío"] = len(filas)

    # 2) Formatos inconsistentes de texto (España / ESPAÑA / espana / ' España ')
    for tabla, col in [("comercio", "pais"), ("sesion", "ip_pais"), ("metodo_pago", "entidad_emisora"),
                       ("metodo_pago", "tipo"), ("transaccion", "estado"), ("dispositivo", "tipo")]:
        candidatas = [f for f in fab.tablas[tabla] if f[col]]
        filas = muestra(candidatas, prob)
        for f in filas:
            f[col] = _variante_texto(rng, f[col])
        resumen[f"{tabla}.{col} formato inconsistente"] = len(filas)

    filas = muestra(fab.tablas["transaccion"], prob)
    for f in filas:
        f["moneda"] = str(rng.choice(["eur", "€", "Euro", "EUR "]))
    resumen["transaccion.moneda formato inconsistente"] = len(filas)

    # 3) Emails mal formados
    candidatas = [c for c in fab.tablas["cliente"] if "@" in c["email"]]
    filas = muestra(candidatas, prob)
    for c in filas:
        c["email"] = c["email"].replace("@", "") if rng.random() < 0.5 else c["email"].replace(".com", "")
    resumen["cliente.email mal formado"] = len(filas)

    # 4) Importes imposibles (0 o negativos) — solo en transacciones normales
    normales = [t for t in fab.tablas["transaccion"] if t["es_fraude"] == 0]
    filas = muestra(normales, prob / 5)
    for t in filas:
        t["cantidad"] = 0.0 if rng.random() < 0.5 else -abs(t["cantidad"])
    resumen["transaccion.cantidad <= 0"] = len(filas)

    # 5) Fechas fuera del periodo (fecha por defecto 1970 o fechas futuras)
    filas = muestra(normales, prob / 10)
    for t in filas:
        if rng.random() < 0.5:
            t["timestamp"] = datetime(1970, 1, 2)
        else:
            t["timestamp"] = datetime.now() + timedelta(days=int(rng.integers(30, 400)))
    resumen["transaccion.timestamp fuera de rango"] = len(filas)

    # 6) Filas duplicadas (la misma transacción cargada dos veces, error de ingesta)
    filas = muestra(fab.tablas["transaccion"], prob / 2)
    fab.tablas["transaccion"].extend(dict(t) for t in filas)
    resumen["transaccion filas duplicadas"] = len(filas)

    return resumen


# ---------------------------------------------------------------------------
# Orquestación
# ---------------------------------------------------------------------------
ORDEN_INSERCION = ["cliente", "comercio", "dispositivo", "patron", "metodo_pago",
                    "canal_pago", "cliente_dispositivo", "sesion", "evento",
                    "transaccion", "devolucion", "alerta"]


def generar_dataset(seed=42, dias=180, proporciones="aleatoria", ensuciar_datos=False,
                    fecha_fin=None, verbose=True):
    """Genera todo el dataset en memoria y devuelve (fab, resumen_suciedad)."""
    log = print if verbose else (lambda *a, **k: None)
    rng = np.random.default_rng(seed)
    random.seed(seed)
    Faker.seed(seed)

    fecha_fin = fecha_fin or (datetime.now() - timedelta(days=15))
    fecha_inicio = fecha_fin - timedelta(days=dias)

    fab = Fabrica()

    log("Generando entidades maestras...")
    paises_por_cliente, perfiles = generar_clientes(fab, rng, fecha_inicio, fecha_fin)
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
    estrenos = generar_estrenos_dispositivo(fab, rng, clientes_ids, dispositivos_personales_ids,
                                            fecha_inicio, fecha_fin)

    log("Generando transacciones legítimas (esto puede tardar un poco)...")
    generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles,
                                    metodos_por_cliente, disp_por_cliente, estrenos,
                                    canales_por_comercio, comercios_ids, paises_por_cliente,
                                    canal_tipo, canal_a_datafono, dispositivos_personales_ids)

    proporcion_fraude = calcular_proporciones_fraude(proporciones, rng)
    log(f"Inyectando casos de fraude (reparto: {proporciones})...")
    for tipo, p in proporcion_fraude.items():
        log(f"    {tipo}: {p:.3f}")
    generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles, metodos_por_cliente,
                    disp_por_cliente, canales_por_comercio, comercios_ids, paises_por_cliente,
                    patron_ids, proporcion_fraude)

    resumen_suciedad = {}
    if ensuciar_datos:
        log("Ensuciando datos (--ensuciar)...")
        resumen_suciedad = ensuciar(fab, rng)
    return fab, resumen_suciedad


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


def resumen(fab, resumen_suciedad=None):
    print("\nResumen del dataset generado:")
    for tabla in ORDEN_INSERCION:
        print(f"  {tabla}: {len(fab.tablas[tabla]):,} filas")
    n_txn = len(fab.tablas["transaccion"])
    n_fraude = sum(t["es_fraude"] for t in fab.tablas["transaccion"])
    print(f"\n  Transacciones totales: {n_txn:,}")
    print(f"  Transacciones fraudulentas (es_fraude=1): {n_fraude:,} ({n_fraude / n_txn * 100:.2f}%)")
    if resumen_suciedad:
        print("\n  Suciedad introducida (lo que debería detectar el informe de limpieza):")
        for problema, n in resumen_suciedad.items():
            print(f"    {problema}: {n:,}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No conecta a ClickHouse, solo genera y resume")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--dias", type=int, default=180)
    parser.add_argument("--proporciones", choices=["igual", "aleatoria"], default="aleatoria",
                         help="como repartir el fraude entre los 9 tipos: aleatoria (por defecto) o igual")
    parser.add_argument("--ensuciar", action="store_true",
                         help="introduce problemas de calidad de datos (vacíos, formatos, duplicados...)")
    args = parser.parse_args()

    fab, resumen_suciedad = generar_dataset(args.seed, args.dias, args.proporciones, args.ensuciar)
    resumen(fab, resumen_suciedad)

    if args.dry_run:
        print("\n--dry-run: no se ha insertado nada en ClickHouse.")
    else:
        print("\nCargando en ClickHouse...")
        cargar_en_clickhouse(fab)
        print("Listo.")


if __name__ == "__main__":
    main()
