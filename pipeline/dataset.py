import argparse
import os
import random
from datetime import datetime, timedelta
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
from faker import Faker

fake = Faker("es_ES")
from config import CONFIG, HOST, PORT, USER, PASSWORD, DATABASE

N_CLIENTES = 2000
N_COMERCIOS = 30
N_DISPOSITIVOS = 2600
TRANSACCIONES_DIA_MEDIA = 200
TASA_FRAUDE_OBJETIVO = 0.02

TIPOS_FRAUDE = ["pico_gasto", "card_testing", "smurfing", "account_takeover", "ip_sospechosa",
                "dispositivo_nuevo", "devolucion_abusiva", "bot", "bust_out"]

TXNS_POR_CASO = {
    "pico_gasto": 1, "card_testing": 7, "smurfing": 4, "account_takeover": 1,
    "ip_sospechosa": 1, "dispositivo_nuevo": 1, "devolucion_abusiva": 3,
    "bot": 1, "bust_out": 1,
}

PAISES_HABITUALES = ["España", "Francia", "Alemania", "Portugal", "Italia",
                     "Reino Unido", "Países Bajos", "Bélgica", "Irlanda", "Polonia"]
PAISES_HABITUALES_PESOS = [0.50, 0.10, 0.09, 0.07, 0.07, 0.06, 0.04, 0.03, 0.02, 0.02]
PAISES_RAROS = ["Rusia", "Nigeria", "Vietnam", "Ucrania", "Indonesia", "Filipinas",
                "China", "Pakistán", "Brasil", "India", "Sudáfrica", "Egipto"]

DIAS_SIN_FRAUDE = 30
MARGEN_FINAL_DIAS = {"devolucion_abusiva": 25}

MOTIVOS_DEVOLUCION = ["producto no conforme", "producto no recibido", "talla incorrecta",
                      "cargo duplicado", "cancelación del pedido"]

P_COMPRA_GRANDE_LEGIT = 0.015   
P_DISPOSITIVO_NUEVO_LEGIT = 0.007  
P_CAMBIO_DATO_LEGIT = 0.01       
P_RAFAGA_PEQUENA_LEGIT = 0.004  
P_DOBLE_CLIC_LEGIT = 0.03
P_VARIAS_COMPRAS_LEGIT = 0.05
P_VIAJE_LEGIT = 0.10             
P_PAIS_RARO_LEGIT = 0.005        


class Fabrica:
    def __init__(self):
        self._contadores = {}
        self.tablas = {
            "cliente": [], "metodo_pago": [], "comercio": [], "canal_pago": [],
            "dispositivo": [], "patron": [], "cliente_dispositivo": [],
            "sesion": [], "evento": [], "transaccion": [], "devolucion": [], "analista": [], "alerta": [],
        }

    def siguiente_id(self, tabla):
        self._contadores[tabla] = self._contadores.get(tabla, 0) + 1
        return self._contadores[tabla]


def calcular_proporciones_fraude(modo, rng):
    if modo == "igual":
        p = 1.0 / len(TIPOS_FRAUDE)
        return {tipo: p for tipo in TIPOS_FRAUDE}
    if modo == "aleatoria":
        valores = rng.dirichlet(np.ones(len(TIPOS_FRAUDE)))
        return dict(zip(TIPOS_FRAUDE, valores))
    raise ValueError(f"modo de proporciones desconocido: {modo}")


def generar_clientes(fab, rng, fecha_inicio):
    perfiles = {}
    for _ in range(N_CLIENTES):
        cid = fab.siguiente_id("cliente")
        nombre = fake.name()
        pais = str(rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS))
        fab.tablas["cliente"].append({
            "cliente_id": cid,
            "nombre_completo": nombre,
            "email": f"{nombre.lower().replace(' ', '.')}{cid}@correo.com",
            "pais": pais,
            "fecha_alta": fecha_inicio - timedelta(days=int(rng.integers(0, 700))),
        })
        perfiles[cid] = {
            "pais": pais,
            "mu": float(rng.normal(3.4, 0.5)),       
            "sigma": float(rng.uniform(0.4, 0.9)),   
            "actividad": float(min(rng.pareto(1.5) + 0.2, 5.0)),  # con tope: nadie compra 25 veces al día  
            "hora_centro": float(rng.choice([9, 13, 18, 21], p=[0.2, 0.3, 0.3, 0.2])),
            "viajero": bool(rng.random() < 0.15),
            "usa_vpn": bool(rng.random() < 0.08),
        }
    return perfiles


def generar_comercios(fab, rng):
    categorias = ["electrónica", "moda", "alimentación", "viajes", "hogar", "ocio",
                  "deporte", "belleza", "juguetería", "librería", "mascotas",
                  "bricolaje", "automoción", "joyería", "farmacia", "restauración"]
    for _ in range(N_COMERCIOS):
        cid = fab.siguiente_id("comercio")
        fab.tablas["comercio"].append({
            "comercio_id": cid,
            "nombre": fake.company(),
            "categoria_negocio": str(rng.choice(categorias)),
            "pais": str(rng.choice(PAISES_HABITUALES, p=PAISES_HABITUALES_PESOS)),
            "ciudad": fake.city(),
        })


def generar_dispositivos(fab, rng):
    tipos = {
        "movil": (["iPhone 14", "Samsung Galaxy S23", "Xiaomi 13"], ["iOS 17", "Android 14"]),
        "ordenador": (["MacBook Pro", "Dell XPS", "HP Pavilion"], ["macOS 14", "Windows 11", "Ubuntu 22.04"]),
        "tablet": (["iPad Air", "Samsung Tab S9"], ["iOS 17", "Android 14"]),
    }
    for _ in range(N_DISPOSITIVOS):
        did = fab.siguiente_id("dispositivo")
        tipo = str(rng.choice(list(tipos.keys()), p=[0.60, 0.28, 0.12]))
        modelos, sistemas = tipos[tipo]
        fab.tablas["dispositivo"].append({
            "dispositivo_id": did,
            "tipo": tipo,
            "modelo": str(rng.choice(modelos)),
            "sistema_operativo": str(rng.choice(sistemas)),
        })


def generar_patrones(fab):
    patrones = [
        ("Pico de gasto anómalo", "Cantidad muy por encima de la media histórica del cliente",
         "importe > 4 veces el gasto medio del cliente"),
        ("Card testing", "Ráfaga de importes muy bajos en poco tiempo con el mismo método de pago",
         ">=3 transacciones del mismo metodo_id, importe <5€, en menos de 15 minutos"),
        ("Estructuración (smurfing)", "Varios pagos repetidos justo por debajo de un umbral redondo",
         ">=3 pagos del mismo metodo_id entre 850€ y 999€ en pocas horas"),
        ("Account takeover", "Intentos de login fallidos, cambio de dato y compra en la misma sesión",
         "cambio_dato + intentos de login >=2 + transacción alta en la misma sesión"),
        ("IP/geolocalización sospechosa", "Sesión desde un país distinto al habitual del cliente",
         "ip_pais distinto al pais del cliente, especialmente con proxy_vpn=true"),
        ("Dispositivo nuevo de alto riesgo", "Compra de importe alto desde un dispositivo nunca visto",
         "dispositivo no presente en cliente_dispositivo para ese cliente e importe alto"),
        ("Abuso de devoluciones", "Varias compras seguidas de devolución en un plazo muy corto",
         ">=2 devoluciones del mismo cliente en menos de 5 días tras la compra"),
        ("Actividad tipo bot", "Eventos consecutivos a una velocidad no humana",
         ">=4 eventos en la misma sesión separados por <3 segundos"),
        ("Bust-out", "Historial de compras pequeñas seguido de un cargo cercano al límite",
         "cargo entre el 85% y 98% del limite_credito tras semanas de compras pequeñas"),
    ]
    for nombre, descripcion, condicion in patrones:
        fab.tablas["patron"].append({
            "patron_id": fab.siguiente_id("patron"), "nombre": nombre,
            "descripcion": descripcion, "condicion": condicion,
        })


def generar_analistas(fab, rng, fecha_inicio, n=6):
    for _ in range(n):
        aid = fab.siguiente_id("analista")
        nombre = fake.name()
        fab.tablas["analista"].append({
            "analista_id": aid,
            "nombre": nombre,
            "email": f"{nombre.lower().replace(' ', '.')}@antifraude.com",
            "nivel": str(rng.choice(["junior", "senior"], p=[0.6, 0.4])),
            "fecha_alta": fecha_inicio - timedelta(days=int(rng.integers(30, 1000))),
            "activo": True,
        })


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
                "fecha_expiracion": (fecha_ref + timedelta(days=int(rng.integers(365, 1460)))).date(),
                "limite_credito": float(rng.integers(1000, 15000)) if tipo == "credito" else None,
                "estado": "activa",
            })
            por_cliente[cid].append(mid)
    return por_cliente


def generar_canales_pago(fab, rng, comercios_ids):
    por_comercio = {cid: [] for cid in comercios_ids}
    canal_tipo = {}
    for cid in comercios_ids:
        tipos = [str(rng.choice(["web", "app"]))]
        if rng.random() < 0.6:
            tipos.append("datafono")
        for tipo in tipos:
            chid = fab.siguiente_id("canal_pago")
            fab.tablas["canal_pago"].append({
                "canal_id": chid,
                "comercio_id": cid,
                "tipo": tipo,
                "ubicacion": "online" if tipo != "datafono" else fake.city(),
            })
            por_comercio[cid].append(chid)
            canal_tipo[chid] = tipo
    return por_comercio, canal_tipo


def generar_cliente_dispositivo(fab, rng, clientes_ids, dispositivos_ids):
    por_cliente = {cid: [] for cid in clientes_ids}
    libres = list(dispositivos_ids)
    rng.shuffle(libres)
    puntero = 0
    for cid in clientes_ids:
        for _ in range(int(rng.integers(1, 4))):
            if puntero >= len(libres):
                puntero = 0
            did = int(libres[puntero])
            puntero += 1
            fab.tablas["cliente_dispositivo"].append({"cliente_id": cid, "dispositivo_id": did})
            por_cliente[cid].append(did)
    return por_cliente


def ip_aleatoria(rng):
    return ".".join(str(int(rng.integers(1, 255))) for _ in range(4))


def crear_sesion(fab, rng, cliente_id, dispositivo_id, momento, pais_ip,
                 proxy_vpn=False, intentos=1, resultado="exitoso"):
    sid = fab.siguiente_id("sesion")
    fab.tablas["sesion"].append({
        "sesion_id": sid, "cliente_id": cliente_id, "dispositivo_id": dispositivo_id,
        "ip_sesion": ip_aleatoria(rng), "ip_pais": pais_ip, "proxy_vpn": bool(proxy_vpn),
        "num_intentos_login": int(intentos), "resultado_login": resultado, "timestamp": momento,
    })
    t = momento - timedelta(seconds=int(intentos) * 8)
    for _ in range(int(intentos) - 1):
        crear_evento(fab, sid, "login_fallido", t, "credenciales incorrectas")
        t += timedelta(seconds=8)
    crear_evento(fab, sid, "login_exitoso" if resultado == "exitoso" else "login_fallido", t)
    return sid


def crear_evento(fab, sesion_id, tipo, momento, detalle=""):
    fab.tablas["evento"].append({
        "evento_id": fab.siguiente_id("evento"), "sesion_id": sesion_id,
        "tipo": tipo, "timestamp": momento, "detalle": detalle,
    })


def crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad,
                      estado="aprobada", tipo_operacion="compra", metodo_auth="3D-secure",
                      moneda="EUR", es_fraude=0, tipo_fraude=None):
    tid = fab.siguiente_id("transaccion")
    fab.tablas["transaccion"].append({
        "transaccion_id": tid, "metodo_id": metodo_id, "canal_id": canal_id,
        "dispositivo_id": dispositivo_id, "sesion_id": sesion_id, "timestamp": momento,
        "cantidad": round(float(cantidad), 2), "moneda": moneda, "tipo_operacion": tipo_operacion,
        "metodo_autenticacion": metodo_auth, "estado": estado,
        "es_fraude": int(es_fraude), "tipo_fraude": tipo_fraude,
    })
    return tid


def crear_devolucion(fab, rng, transaccion, horas_min, horas_max, parcial=False):
    importe = transaccion["cantidad"] * (float(rng.uniform(0.2, 0.8)) if parcial else 1.0)
    fab.tablas["devolucion"].append({
        "devolucion_id": fab.siguiente_id("devolucion"),
        "transaccion_id": transaccion["transaccion_id"],
        "tipo": "reembolso_parcial" if parcial else "reembolso_total",
        "motivo": str(rng.choice(MOTIVOS_DEVOLUCION)),
        "importe": round(importe, 2),
        "fecha": transaccion["timestamp"] + timedelta(hours=float(rng.uniform(horas_min, horas_max))),
        "estado": "procesada",
    })


def auth_online(rng):
    return str(rng.choice(["3D-secure", "biometria", "ninguno"], p=[0.70, 0.20, 0.10]))


def momento_aleatorio(rng, fecha_inicio, fecha_fin):
    delta = (fecha_fin - fecha_inicio).total_seconds()
    return fecha_inicio + timedelta(seconds=float(rng.uniform(0, delta)))


def elegir_dispositivo_nuevo(rng, cid, disp_por_cliente, todos_dispositivos):
    propios = set(disp_por_cliente[cid])
    while True:
        did = int(rng.choice(todos_dispositivos))
        if did not in propios:
            return did


def generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles,
                                   favoritos, metodos_por_cliente, disp_por_cliente,
                                   canales_por_comercio, canal_tipo, comercios_ids, todos_dispositivos):
    pesos = np.array([perfiles[c]["actividad"] for c in clientes_ids])
    pesos = pesos / pesos.sum()
    n_dias = (fecha_fin - fecha_inicio).days

    for dia in range(n_dias):
        dia_inicio = fecha_inicio + timedelta(days=dia)
        for _ in range(int(rng.poisson(TRANSACCIONES_DIA_MEDIA))):
            cid = int(rng.choice(clientes_ids, p=pesos))
            perfil = perfiles[cid]
            metodo_id = int(rng.choice(metodos_por_cliente[cid]))

            comercio_id = int(rng.choice(favoritos[cid])) if rng.random() < 0.8 else int(rng.choice(comercios_ids))
            canal_id = int(rng.choice(canales_por_comercio[comercio_id]))

            if rng.random() < 0.85:
                hora = (perfil["hora_centro"] + rng.normal(0, 2.5)) % 24
            else:
                hora = rng.uniform(0, 24)
            momento = dia_inicio + timedelta(hours=float(hora))

            cantidad = float(rng.lognormal(perfil["mu"], perfil["sigma"]))
            if rng.random() < P_COMPRA_GRANDE_LEGIT:
                cantidad *= float(rng.uniform(5, 15))
            estado = "aprobada" if rng.random() < 0.96 else "rechazada"

            if canal_tipo[canal_id] == "datafono":
                crear_transaccion(fab, None, metodo_id, canal_id, None, momento, cantidad, estado=estado,
                                  metodo_auth=str(rng.choice(["pin", "contactless"], p=[0.4, 0.6])))
                continue

            if rng.random() < P_DISPOSITIVO_NUEVO_LEGIT:
                dispositivo_id = elegir_dispositivo_nuevo(rng, cid, disp_por_cliente, todos_dispositivos)
            else:
                dispositivo_id = int(rng.choice(disp_por_cliente[cid]))

            pais_ip = perfil["pais"]
            if perfil["viajero"] and rng.random() < P_VIAJE_LEGIT:
                pais_ip = str(rng.choice(PAISES_HABITUALES))
            if rng.random() < P_PAIS_RARO_LEGIT:
                pais_ip = str(rng.choice(PAISES_RAROS))

            vpn = (perfil["usa_vpn"] and rng.random() < 0.5) or rng.random() < 0.01
            intentos = int(rng.choice([1, 2, 3, 4, 5, 6], p=[0.875, 0.08, 0.03, 0.01, 0.004, 0.001]))

            sesion_id = crear_sesion(fab, rng, cid, dispositivo_id, momento - timedelta(minutes=float(rng.uniform(1, 15))),
                                     pais_ip=pais_ip, proxy_vpn=vpn, intentos=intentos)
            if rng.random() < P_CAMBIO_DATO_LEGIT:
                crear_evento(fab, sesion_id, "cambio_dato", momento - timedelta(minutes=1), "cambio de contraseña")
            crear_evento(fab, sesion_id, "intento_pago", momento, "pago iniciado")
            if rng.random() < P_DOBLE_CLIC_LEGIT:
                crear_evento(fab, sesion_id, "intento_pago", momento + timedelta(seconds=float(rng.uniform(0.3, 2))),
                             "pago iniciado")
            crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, momento, cantidad,
                              estado=estado, metodo_auth=auth_online(rng))

            if rng.random() < P_VARIAS_COMPRAS_LEGIT:
                t = momento
                for _ in range(int(rng.integers(1, 3))):
                    t += timedelta(minutes=float(rng.uniform(1, 20)))
                    crear_evento(fab, sesion_id, "intento_pago", t, "pago iniciado")
                    crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t,
                                      float(rng.lognormal(perfil["mu"], perfil["sigma"])),
                                      metodo_auth=auth_online(rng))

            if rng.random() < P_RAFAGA_PEQUENA_LEGIT:
                t = momento
                for _ in range(int(rng.integers(2, 5))):
                    t += timedelta(seconds=float(rng.uniform(20, 400)))
                    crear_evento(fab, sesion_id, "intento_pago", t, "pago iniciado")
                    crear_transaccion(fab, sesion_id, metodo_id, canal_id, dispositivo_id, t,
                                      float(rng.uniform(1, 6)), metodo_auth=auth_online(rng))

    aprobadas = [t for t in fab.tablas["transaccion"] if t["estado"] == "aprobada" and t["cantidad"] > 30]
    n_devol = int(len(aprobadas) * 0.02)
    for idx in rng.choice(len(aprobadas), size=n_devol, replace=False):
        crear_devolucion(fab, rng, aprobadas[int(idx)], horas_min=6, horas_max=14 * 24,
                         parcial=bool(rng.random() < 0.3))


def antes(rng, momento):
    return momento - timedelta(minutes=float(rng.uniform(1, 15)))


def hora_fraude(rng, momento):
    if rng.random() < 0.4:
        return momento.replace(hour=int(rng.integers(0, 6)), minute=int(rng.integers(0, 60)))
    return momento


def caso_pico_gasto(fab, rng, c):
    sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, c["momento"]), pais_ip=c["pais"],
                       proxy_vpn=rng.random() < 0.15, intentos=int(rng.choice([1, 2], p=[0.8, 0.2])))
    crear_evento(fab, sid, "intento_pago", c["momento"], "pago iniciado")
    cantidad = np.exp(c["mu"]) * float(rng.uniform(4, 20))  
    crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], c["momento"], cantidad,
                      metodo_auth=auth_online(rng), es_fraude=1)


def caso_card_testing(fab, rng, c):
    disp = c["disp_nuevo"] if rng.random() < 0.5 else c["disp"]
    pais = str(rng.choice(PAISES_RAROS)) if rng.random() < 0.5 else c["pais"]
    sid = crear_sesion(fab, rng, c["cid"], disp, antes(rng, c["momento"]), pais_ip=pais,
                       proxy_vpn=rng.random() < 0.4)
    t = c["momento"]
    p_rechazo = float(rng.uniform(0.4, 0.85))
    for _ in range(int(rng.integers(3, 12))):
        crear_evento(fab, sid, "intento_pago", t, "pago iniciado")
        crear_transaccion(fab, sid, c["metodo"], c["canal"], disp, t, float(rng.uniform(0.5, 5.0)),
                          estado="rechazada" if rng.random() < p_rechazo else "aprobada",
                          metodo_auth="ninguno" if rng.random() < 0.5 else "3D-secure", es_fraude=1)
        t += timedelta(seconds=float(rng.uniform(5, 300)))


def caso_smurfing(fab, rng, c):
    t = c["momento"]
    for _ in range(int(rng.integers(3, 6))):
        sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, t), pais_ip=c["pais"])
        crear_evento(fab, sid, "intento_pago", t, "pago iniciado")
        crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], t,
                          1000 - float(rng.uniform(5, 150)), metodo_auth=auth_online(rng), es_fraude=1)
        t += timedelta(minutes=float(rng.uniform(10, 240)))


def caso_account_takeover(fab, rng, c):
    disp = c["disp_nuevo"] if rng.random() < 0.6 else c["disp"]
    pais = str(rng.choice(PAISES_RAROS)) if rng.random() < 0.6 else c["pais"]  
    sid = crear_sesion(fab, rng, c["cid"], disp, antes(rng, c["momento"]), pais_ip=pais,
                       proxy_vpn=rng.random() < 0.6, intentos=int(rng.integers(1, 8)))
    t = c["momento"] + timedelta(minutes=float(rng.uniform(0.5, 10)))
    if rng.random() < 0.7:
        crear_evento(fab, sid, "cambio_dato", t, "cambio de contraseña")
        t += timedelta(minutes=float(rng.uniform(0.5, 20)))
    crear_evento(fab, sid, "intento_pago", t, "pago iniciado")
    cantidad = np.exp(c["mu"]) * float(rng.uniform(3, 25))
    crear_transaccion(fab, sid, c["metodo"], c["canal"], disp, t, cantidad,
                      metodo_auth=auth_online(rng), es_fraude=1)


def caso_ip_sospechosa(fab, rng, c):
    sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, c["momento"]), pais_ip=str(rng.choice(PAISES_RAROS)),
                       proxy_vpn=rng.random() < 0.7)
    crear_evento(fab, sid, "intento_pago", c["momento"], "pago iniciado")
    cantidad = np.exp(c["mu"]) * float(rng.uniform(1, 6))  
    crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], c["momento"], cantidad,
                      metodo_auth=auth_online(rng), es_fraude=1)


def caso_dispositivo_nuevo(fab, rng, c):
    sid = crear_sesion(fab, rng, c["cid"], c["disp_nuevo"], antes(rng, c["momento"]), pais_ip=c["pais"],
                       intentos=int(rng.choice([1, 2], p=[0.7, 0.3])))
    crear_evento(fab, sid, "intento_pago", c["momento"], "pago iniciado")
    cantidad = np.exp(c["mu"]) * float(rng.uniform(2, 15))
    crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp_nuevo"], c["momento"], cantidad,
                      metodo_auth=auth_online(rng), es_fraude=1)


def caso_devolucion_abusiva(fab, rng, c):
    t = c["momento"]
    for _ in range(int(rng.integers(2, 5))):
        sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, t), pais_ip=c["pais"])
        crear_evento(fab, sid, "intento_pago", t, "pago iniciado")
        tid = crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], t,
                                float(rng.uniform(50, 400)), metodo_auth=auth_online(rng), es_fraude=1)
        crear_devolucion(fab, rng, fab.tablas["transaccion"][-1], horas_min=6, horas_max=5 * 24)
        t += timedelta(days=float(rng.uniform(1, 6)))


def caso_bot(fab, rng, c):
    sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, c["momento"]), pais_ip=c["pais"],
                       proxy_vpn=rng.random() < 0.3)
    t = c["momento"]
    for _ in range(int(rng.integers(4, 9))):
        crear_evento(fab, sid, str(rng.choice(["intento_pago", "cambio_dato"], p=[0.8, 0.2])), t)
        t += timedelta(seconds=float(rng.uniform(0.2, 3)))
    crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], t, float(rng.uniform(20, 300)),
                      metodo_auth="ninguno" if rng.random() < 0.5 else "3D-secure", es_fraude=1)


def caso_bust_out(fab, rng, c, fecha_inicio, limite):
    t = fecha_inicio
    while t < c["momento"] - timedelta(days=5):
        sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, t), pais_ip=c["pais"])
        crear_evento(fab, sid, "intento_pago", t, "pago iniciado")
        crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], t, float(rng.uniform(10, 80)),
                          metodo_auth=auth_online(rng))
        t += timedelta(days=float(rng.uniform(3, 10)))
    sid = crear_sesion(fab, rng, c["cid"], c["disp"], antes(rng, c["momento"]), pais_ip=c["pais"])
    crear_evento(fab, sid, "intento_pago", c["momento"], "pago iniciado")
    crear_transaccion(fab, sid, c["metodo"], c["canal"], c["disp"], c["momento"],
                      float(limite) * float(rng.uniform(0.85, 0.98)), metodo_auth=auth_online(rng), es_fraude=1)


CASOS = {
    "pico_gasto": caso_pico_gasto, "card_testing": caso_card_testing, "smurfing": caso_smurfing,
    "account_takeover": caso_account_takeover, "ip_sospechosa": caso_ip_sospechosa,
    "dispositivo_nuevo": caso_dispositivo_nuevo, "devolucion_abusiva": caso_devolucion_abusiva,
    "bot": caso_bot,
}


def generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles, metodos_por_cliente,
                   disp_por_cliente, canales_por_comercio, canal_tipo, comercios_ids,
                   todos_dispositivos, proporcion_fraude):
    n_txn_estimado = TRANSACCIONES_DIA_MEDIA * (fecha_fin - fecha_inicio).days
    n_fraude_objetivo = int(n_txn_estimado * TASA_FRAUDE_OBJETIVO / (1 - TASA_FRAUDE_OBJETIVO))
    metodos = {m["metodo_id"]: m for m in fab.tablas["metodo_pago"]}

    for tipo, proporcion in proporcion_fraude.items():
        n_casos = max(1, int(n_fraude_objetivo * proporcion) // TXNS_POR_CASO[tipo])
        for _ in range(n_casos):
            cid = int(rng.choice(clientes_ids))
            comercio_id = int(rng.choice(comercios_ids))
            canales_online = [ch for ch in canales_por_comercio[comercio_id] if canal_tipo[ch] != "datafono"]
            c = {
                "cid": cid,
                "metodo": int(rng.choice(metodos_por_cliente[cid])),
                "disp": int(rng.choice(disp_por_cliente[cid])),
                "disp_nuevo": elegir_dispositivo_nuevo(rng, cid, disp_por_cliente, todos_dispositivos),
                "canal": int(rng.choice(canales_online)),  
                "momento": hora_fraude(rng, momento_aleatorio(
                    rng, fecha_inicio + timedelta(days=DIAS_SIN_FRAUDE),
                    fecha_fin - timedelta(days=MARGEN_FINAL_DIAS.get(tipo, 1)))),
                "pais": perfiles[cid]["pais"],
                "mu": perfiles[cid]["mu"],
            }
            n_antes = len(fab.tablas["transaccion"])
            if tipo == "bust_out":
                creditos = [m for m in metodos_por_cliente[cid] if metodos[m]["limite_credito"]]
                if not creditos:
                    continue
                c["metodo"] = int(rng.choice(creditos))
                caso_bust_out(fab, rng, c, fecha_inicio, metodos[c["metodo"]]["limite_credito"])
            else:
                CASOS[tipo](fab, rng, c)
            for t in fab.tablas["transaccion"][n_antes:]:
                if t["es_fraude"] == 1:
                    t["tipo_fraude"] = tipo

ORDEN_INSERCION = ["cliente", "comercio", "dispositivo", "patron", "metodo_pago",
                   "canal_pago", "cliente_dispositivo", "sesion", "evento",
                   "transaccion", "devolucion", "analista", "alerta"]


def cargar_en_clickhouse(fab):
    import clickhouse_connect
    client = clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE
    )
    for tabla in ORDEN_INSERCION:
        client.command(f"TRUNCATE TABLE IF EXISTS {tabla}")  
        filas = fab.tablas[tabla]
        if not filas:
            print(f"  {tabla}: vacía")
            continue
        columnas = list(filas[0].keys())
        datos = [[fila[col] for col in columnas] for fila in filas]  
        client.insert(tabla, datos, column_names=columnas)
        print(f"  {tabla}: {len(filas):,} filas cargadas")


def resumen(fab):
    print("\nResumen del dataset generado:")
    for tabla in ORDEN_INSERCION:
        print(f"  {tabla}: {len(fab.tablas[tabla]):,} filas")
    txns = fab.tablas["transaccion"]
    n_fraude = sum(t["es_fraude"] for t in txns)
    print(f"\n  Transacciones totales: {len(txns):,}")
    print(f"  Transacciones fraudulentas: {n_fraude:,} ({n_fraude / len(txns) * 100:.2f}%)")
    por_tipo = {}
    for t in txns:
        if t["es_fraude"]:
            por_tipo[t["tipo_fraude"]] = por_tipo.get(t["tipo_fraude"], 0) + 1
    for tipo, n in sorted(por_tipo.items(), key=lambda x: -x[1]):
        print(f"    {tipo}: {n}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="No conecta a ClickHouse, solo genera y resume")
    parser.add_argument("--seed", type=int, default=CONFIG['semilla'])
    parser.add_argument("--dias", type=int, default=180)
    parser.add_argument("--proporciones", choices=["igual", "aleatoria"], default="igual")
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    random.seed(args.seed)
    Faker.seed(args.seed)

    fecha_fin = datetime(2026, 9, 1)  
    fecha_inicio = fecha_fin - timedelta(days=args.dias)

    fab = Fabrica()
    print("Generando entidades maestras...")
    perfiles = generar_clientes(fab, rng, fecha_inicio)
    generar_comercios(fab, rng)
    generar_dispositivos(fab, rng)
    generar_patrones(fab)

    clientes_ids = [c["cliente_id"] for c in fab.tablas["cliente"]]
    comercios_ids = [c["comercio_id"] for c in fab.tablas["comercio"]]
    todos_dispositivos = [d["dispositivo_id"] for d in fab.tablas["dispositivo"]]
    favoritos = {cid: [int(x) for x in rng.choice(comercios_ids, size=int(rng.integers(3, 7)), replace=False)]
                 for cid in clientes_ids}

    metodos_por_cliente = generar_metodos_pago(fab, rng, clientes_ids, fecha_inicio)
    canales_por_comercio, canal_tipo = generar_canales_pago(fab, rng, comercios_ids)
    disp_por_cliente = generar_cliente_dispositivo(fab, rng, clientes_ids, todos_dispositivos)

    print("Generando transacciones legítimas...")
    generar_transacciones_normales(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles, favoritos,
                                   metodos_por_cliente, disp_por_cliente, canales_por_comercio,
                                   canal_tipo, comercios_ids, todos_dispositivos)

    proporcion_fraude = calcular_proporciones_fraude(args.proporciones, rng)
    print(f"Inyectando casos de fraude (reparto: {args.proporciones})...")
    generar_fraude(fab, rng, fecha_inicio, fecha_fin, clientes_ids, perfiles, metodos_por_cliente,
                   disp_por_cliente, canales_por_comercio, canal_tipo, comercios_ids,
                   todos_dispositivos, proporcion_fraude)

    generar_analistas(fab, rng, fecha_inicio)

    resumen(fab)

    print("\nCargando en ClickHouse...")
    cargar_en_clickhouse(fab)
    print("Listo.")


if __name__ == "__main__":
    main()