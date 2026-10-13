import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dataset as ds
from config import CONFIG, DATABASE, HOST, PASSWORD, PORT, USER

TABLAS_LOTE = ["sesion", "evento", "devolucion", "transaccion"]   # transaccion SIEMPRE la última
# casos de fraude que caben en un lote corto (bust_out y devolucion_abusiva duran semanas)
TIPOS_FRAUDE_LOTE = ["pico_gasto", "card_testing", "smurfing", "account_takeover",
                     "ip_sospechosa", "dispositivo_nuevo", "bot"]
AJUSTES_MUTACION = {"mutations_sync": 2}


def conectar():
    import clickhouse_connect
    client = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                           database=DATABASE)
    if not client.command("SELECT count() FROM transaccion_bruta"):
        raise SystemExit("transaccion_bruta está vacía: carga antes el dataset inicial (dataset.py)")
    return client


def uno(client, sql):
    return client.query(sql).result_rows[0][0]


def estado_actual(client):
    """Últimos ids de lo que está cargado del todo, a partir de transaccion_bruta (la última tabla
    que se inserta). Lo que haya por encima en las otras tablas es de un lote a medias."""
    ult = {"transaccion": uno(client, "SELECT max(transaccion_id) FROM transaccion_bruta"),
           "sesion": uno(client, "SELECT max(sesion_id) FROM transaccion_bruta")}
    ult["evento"] = uno(client, f"SELECT max(evento_id) FROM evento WHERE sesion_id <= {ult['sesion']}")
    ult["devolucion"] = uno(client, "SELECT ifNull(max(devolucion_id), 0) FROM devolucion "
                                    f"WHERE transaccion_id <= {ult['transaccion']}")
    return ult


def borrar_restos(client, ult):
    pk = {"sesion": "sesion_id", "evento": "evento_id", "devolucion": "devolucion_id"}
    for tabla, col in pk.items():
        n = uno(client, f"SELECT count() FROM {tabla} WHERE {col} > {ult[tabla]}")
        if n:
            client.command(f"ALTER TABLE {tabla} DELETE WHERE {col} > {ult[tabla]}",
                           settings=AJUSTES_MUTACION)
            print(f"  {tabla}: {n:,} filas de un lote anterior a medias borradas")


def recortar(fab, fin):
    """Quita lo que caería después del final del lote (aún no ha ocurrido): transacciones y eventos
    posteriores, sesiones que se quedan sin ninguna transacción y sus devoluciones."""
    t = fab.tablas
    t["transaccion"] = [x for x in t["transaccion"] if x["timestamp"] < fin]
    con_compra = {x["sesion_id"] for x in t["transaccion"]}
    t["sesion"] = [x for x in t["sesion"] if x["sesion_id"] in con_compra]
    sesiones = {x["sesion_id"] for x in t["sesion"]}
    t["evento"] = [x for x in t["evento"] if x["sesion_id"] in sesiones and x["timestamp"] < fin]
    ids = {x["transaccion_id"] for x in t["transaccion"]}
    t["devolucion"] = [x for x in t["devolucion"] if x["transaccion_id"] in ids and x["fecha"] < fin]


def generar_fraude_lote(fab, rng, m, inicio, fin, libres):
    """Unos pocos casos de fraude conocidos, en la proporción del config.yaml."""
    n_legitimas = len(fab.tablas["transaccion"])
    tasa = ds.TASA_FRAUDE_OBJETIVO
    txns_por_caso = np.mean([ds.TXNS_POR_CASO[tipo] for tipo in TIPOS_FRAUDE_LOTE])
    n_casos = int(rng.poisson(n_legitimas * tasa / (1 - tasa) / txns_por_caso))
    for _ in range(n_casos):
        tipo = str(rng.choice(TIPOS_FRAUDE_LOTE))
        cid = int(rng.choice(m.clientes_ids))
        comercio_id = int(rng.choice(m.comercios_ids))
        canales_online = [ch for ch in m.canales_por_comercio[comercio_id] if m.canal_tipo[ch] != "datafono"]
        c = {
            "cid": cid,
            "metodo": int(rng.choice(m.metodos_por_cliente[cid])),
            "disp": int(rng.choice(m.disp_por_cliente[cid])),
            "disp_nuevo": (int(rng.choice(m.dispositivos_estafador))
                           if rng.random() < ds.P_USA_DISPOSITIVO_ESTAFADOR
                           else ds.elegir_dispositivo_nuevo(rng, libres)),
            "canal": int(rng.choice(canales_online)),
            "momento": ds.hora_fraude(rng, ds.momento_aleatorio(rng, inicio, fin)),
            "pais": m.perfiles[cid]["pais"],
            "mu": m.perfiles[cid]["mu"],
            "fecha_fin": fin,
        }
        n_antes = len(fab.tablas["transaccion"])
        ds.CASOS[tipo](fab, rng, c)
        for t in fab.tablas["transaccion"][n_antes:]:
            if t["es_fraude"] == 1:
                t["tipo_fraude"] = tipo
                if tipo != "card_testing" and rng.random() < ds.P_RECHAZO_FRAUDE:
                    t["estado"] = "rechazada"
    return n_casos


def cargar(client, fab):
    for tabla in TABLAS_LOTE:
        destino = "transaccion_bruta" if tabla == "transaccion" else tabla
        filas = fab.tablas[tabla]
        if not filas:
            print(f"  {destino}: sin filas nuevas")
            continue
        if tabla == "transaccion":
            for fila in filas:
                if isinstance(fila["timestamp"], datetime):   # sin ensuciar: formato habitual
                    fila["timestamp"] = fila["timestamp"].strftime(ds.FORMATO_FECHA)
        columnas = list(filas[0].keys())
        datos = [[ds.como_utc(fila[col]) for col in columnas] for fila in filas]
        client.insert(destino, datos, column_names=columnas)
        print(f"  {destino}: {len(filas):,} filas nuevas")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias", type=int, default=1, help="días de transacciones del lote (1 por defecto)")
    args = parser.parse_args()

    client = conectar()
    ult = estado_actual(client)
    borrar_restos(client, ult)
    ultima_sesion = uno(client, f"SELECT max(timestamp) FROM sesion WHERE sesion_id <= {ult['sesion']}")
    inicio = datetime.combine(ultima_sesion.date() + timedelta(days=1), datetime.min.time())
    fin = inicio + timedelta(days=args.dias)
    print(f"Lote: {inicio:%Y-%m-%d} a {fin - timedelta(seconds=1):%Y-%m-%d %H:%M:%S} ({args.dias} día/s)")

    # El mundo del dataset inicial, con su semilla (mismo fecha_inicio: mismas fechas de alta)
    semilla = CONFIG["semilla"]
    m = ds.generar_mundo(ds.Fabrica(), ds.sembrar(semilla),
                         ds.FECHA_FIN - timedelta(days=ds.CONFIG_DATASET["dias"]))
    usados = {d for (d,) in client.query("SELECT DISTINCT dispositivo_id FROM sesion").result_rows}
    libres = [d for d in m.dispositivos_sin_dueno if d not in usados]

    # Generador propio del lote: mismo día -> mismo lote
    rng = np.random.default_rng([semilla, inicio.toordinal()])
    fab = ds.Fabrica()
    fab._contadores = {t: int(ult[t]) for t in TABLAS_LOTE}   # los ids siguen tras los existentes
    ds.generar_transacciones_normales(fab, rng, inicio, fin, m.clientes_ids, m.perfiles, m.favoritos,
                                      m.metodos_por_cliente, m.disp_por_cliente, m.canales_por_comercio,
                                      m.canal_tipo, m.comercios_ids, libres)
    n_casos = generar_fraude_lote(fab, rng, m, inicio, fin, libres)
    recortar(fab, fin)

    txns = fab.tablas["transaccion"]
    n_fraude = sum(t["es_fraude"] for t in txns)
    print(f"Generadas {len(txns):,} transacciones ({n_fraude} de fraude en {n_casos} casos, sin confirmar)")

    suciedad = CONFIG.get("suciedad", {})
    if suciedad.get("activar"):
        ds.ensuciar(fab, semilla + inicio.toordinal(), suciedad)

    print("\nCargando en ClickHouse...")
    cargar(client, fab)
    print("Listo. Siguiente paso: carga_transacciones.py")


if __name__ == "__main__":
    main()
