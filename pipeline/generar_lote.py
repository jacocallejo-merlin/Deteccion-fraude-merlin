"""
Añade un día (o --dias) de transacciones nuevas a las tablas de origen, como si llegara el lote
diario del banco: transacciones legítimas, unos pocos casos de fraude conocido y los mismos errores
de formato que el dataset inicial. Después hay que lanzar carga_transacciones.py y el resto del ETL.

También llegan las confirmaciones del banco (contracargos) de ese día: de los fraudes aún sin
confirmar, los de días anteriores y los del propio lote, se confirman los que tocan con el mismo
retraso que en el dataset inicial (dataset.etiquetas en config.yaml). Sin esto, en la operación
diaria el banco no confirmaría nunca nada y el recalibrado solo aprendería de los analistas.

Cada día usa su propia semilla (la del proyecto y su fecha). Con --dias N se generan N días
seguidos, uno tras otro igual que si se lanzara N veces con un día: un lote de N días y N lotes de
un día dan exactamente lo mismo. Así main.py --desde-cero puede reconstruir los días que había
añadido el scoring diario.
"""
import argparse
import copy
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dataset as ds
from carga_transacciones import FECHA
from config import CONFIG, DATABASE, HOST, PASSWORD, PORT, USER

TABLAS_LOTE = ["sesion", "evento", "devolucion", "transaccion"]   # transaccion SIEMPRE la última
# casos de fraude que caben en un lote corto (bust_out y devolucion_abusiva duran semanas)
TIPOS_FRAUDE_LOTE = ["pico_gasto", "card_testing", "smurfing", "account_takeover",
                     "ip_sospechosa", "dispositivo_nuevo", "bot"]
AJUSTES_MUTACION = {"mutations_sync": 2}

_ETIQUETAS = CONFIG["dataset"]["etiquetas"]
P_NUNCA_CONFIRMADO = _ETIQUETAS["p_nunca_confirmado"]
# log del retraso de la confirmación en días (log-normal, igual que dataset.asignar_confirmaciones)
LOG_RETRASO = NormalDist(math.log(_ETIQUETAS["retraso_mediana_dias"]), _ETIQUETAS["retraso_sigma"])


def _cdf_retraso(dias):
    return LOG_RETRASO.cdf(math.log(dias)) if dias > 0 else 0.0


def fecha_confirmacion(transaccion_id, momento, inicio, fin, semilla):
    """Fecha en que el banco confirma un fraude que seguía sin confirmar al empezar el lote, si cae
    dentro del lote (inicio, fin]; si no, None. Se condiciona a lo que ya se sabe: no se había
    confirmado antes de inicio (puede ser de los que no se confirman nunca, o tardar más).
    Mismo fraude y mismo lote -> misma respuesta."""
    a = max((inicio - momento).total_seconds() / 86400, 0.0)   # días que ya lleva sin confirmar
    b = (fin - momento).total_seconds() / 86400
    if b <= 0:
        return None
    fa, fb = _cdf_retraso(a), _cdf_retraso(b)
    sigue_pendiente = P_NUNCA_CONFIRMADO + (1 - P_NUNCA_CONFIRMADO) * (1 - fa)
    p_en_el_lote = (1 - P_NUNCA_CONFIRMADO) * (fb - fa) / sigue_pendiente
    rng = np.random.default_rng([semilla, 2000, int(transaccion_id), inicio.toordinal()])
    if rng.random() >= p_en_el_lote:
        return None
    # retraso condicionado a caer dentro del lote: inversa de la distribución entre fa y fb
    u = min(max(fa + (fb - fa) * rng.random(), 1e-12), 1 - 1e-12)
    fecha = momento + timedelta(days=math.exp(LOG_RETRASO.inv_cdf(u)))
    return min(max(fecha, inicio, momento), fin - timedelta(seconds=1)).replace(microsecond=0)


def confirmar_lote(fab, inicio, fin, semilla):
    """Confirmaciones de los fraudes del propio lote (en Python, antes de cargarlo)."""
    n = 0
    for t in fab.tablas["transaccion"]:
        if t["es_fraude"] == 1:
            t["fecha_confirmacion"] = fecha_confirmacion(t["transaccion_id"], t["timestamp"],
                                                         inicio, fin, semilla)
            n += t["fecha_confirmacion"] is not None
    return n


def confirmar_pendientes(client, inicio, fin, semilla):
    """Confirmaciones que llegan en este lote de fraudes de días anteriores. Se escriben en
    transaccion_bruta (carga_transacciones.py las pasa a transaccion). Las fechas van como segundos
    Unix para no depender de la zona horaria del ordenador.
    Los pendientes se leen de transaccion_bruta con la misma conversión de fechas que
    carga_transacciones.py (las ilegibles, que irían a cuarentena, no cuentan): es lo que tendría
    transaccion tras la carga, sin depender de que se haya lanzado entre un día y el siguiente."""
    pendientes = client.query(
        f"SELECT transaccion_id, toUnixTimestamp(fecha) "
        f"FROM (SELECT transaccion_id, es_fraude, fecha_confirmacion, {FECHA} AS fecha FROM transaccion_bruta) "
        "WHERE es_fraude = 1 AND fecha_confirmacion IS NULL AND fecha IS NOT NULL "
        "AND fecha < {i:DateTime('UTC')}",
        parameters={"i": inicio.replace(tzinfo=timezone.utc)}).result_rows
    confirmadas = {}
    for tid, segundos in pendientes:
        momento = datetime.fromtimestamp(segundos, timezone.utc).replace(tzinfo=None)
        fecha = fecha_confirmacion(tid, momento, inicio, fin, semilla)
        if fecha is not None:
            confirmadas[int(tid)] = int(fecha.replace(tzinfo=timezone.utc).timestamp())
    if confirmadas:
        ids, fechas = list(confirmadas), list(confirmadas.values())
        client.command(
            f"ALTER TABLE transaccion_bruta UPDATE fecha_confirmacion = "
            f"toDateTime(transform(transaccion_id, {ids}, {fechas}, 0), 'UTC') "
            f"WHERE es_fraude = 1 AND fecha_confirmacion IS NULL AND transaccion_id IN {tuple(ids) + (0,)}",
            settings=AJUSTES_MUTACION)
    return len(confirmadas), len(pendientes)


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


def generar_mundo():
    """El mundo del dataset inicial (clientes, tarjetas, dispositivos, comercios), con su semilla
    y la misma fecha de inicio: mismas fechas de alta."""
    return ds.generar_mundo(ds.Fabrica(), ds.sembrar(CONFIG["semilla"]),
                            ds.FECHA_FIN - timedelta(days=ds.CONFIG_DATASET["dias"]))


def lote_de_un_dia(client, mundo):
    """Genera y carga el día siguiente al último que hay. Todo sale de lo que hay en ClickHouse y
    de la semilla de ese día: mismo estado y mismo día -> mismo lote."""
    ult = estado_actual(client)
    borrar_restos(client, ult)
    ultima_sesion = uno(client, f"SELECT max(timestamp) FROM sesion WHERE sesion_id <= {ult['sesion']}")
    inicio = datetime.combine(ultima_sesion.date() + timedelta(days=1), datetime.min.time())
    fin = inicio + timedelta(days=1)
    print(f"\nLote: {inicio:%Y-%m-%d}")

    semilla = CONFIG["semilla"]
    m = copy.deepcopy(mundo)    # cada día parte del mundo recién generado, como un lote suelto
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
    n_conf_lote = confirmar_lote(fab, inicio, fin, semilla)   # antes de ensuciar: necesita las fechas
    print(f"Generadas {len(txns):,} transacciones ({n_fraude} de fraude en {n_casos} casos, "
          f"{n_conf_lote} ya confirmadas por el banco dentro del lote)")

    suciedad = CONFIG.get("suciedad", {})
    if suciedad.get("activar"):
        ds.ensuciar(fab, semilla + inicio.toordinal(), suciedad)

    print("\nCargando en ClickHouse...")
    # Antes que el lote: si la carga falla a medias, al repetir el mismo día sale lo mismo
    n_conf, n_pend = confirmar_pendientes(client, inicio, fin, semilla)
    print(f"  confirmaciones del banco: {n_conf} de {n_pend} fraudes anteriores que seguían sin confirmar")
    cargar(client, fab)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dias", type=int, default=1,
                        help="días seguidos que se generan, cada uno con su semilla (1 por defecto)")
    args = parser.parse_args()

    client = conectar()
    mundo = generar_mundo()      # una sola vez: es lo más lento y no cambia de un día a otro
    for _ in range(args.dias):
        lote_de_un_dia(client, mundo)
    print(f"\nListo: {args.dias} día/s. Siguiente paso: carga_transacciones.py")


if __name__ == "__main__":
    main()
