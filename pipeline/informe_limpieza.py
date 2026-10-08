import clickhouse_connect
from datetime import datetime
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import HOST, PORT, USER, PASSWORD, DATABASE, CARPETA_RESULTADOS

TABLAS = ["cliente", "comercio", "dispositivo", "patron", "analista", "metodo_pago", "canal_pago",
          "cliente_dispositivo", "sesion", "evento", "transaccion", "devolucion", "alerta"]

PK_POR_TABLA = {
    "cliente": "cliente_id", "comercio": "comercio_id", "dispositivo": "dispositivo_id",
    "patron": "patron_id", "analista": "analista_id", "metodo_pago": "metodo_id",
    "canal_pago": "canal_id", "sesion": "sesion_id", "evento": "evento_id",
    "transaccion": "transaccion_id", "devolucion": "devolucion_id", "alerta": "alerta_id",
    "cliente_dispositivo": "cliente_id, dispositivo_id",
}

ESPERADOS = {
    ("metodo_pago", "limite_credito"): "tarjetas de débito",
    ("transaccion", "dispositivo_id"): "pagos presenciales (datáfono)",
    ("transaccion", "sesion_id"): "pagos presenciales (datáfono)",
    ("transaccion", "tipo_fraude"): "transacciones legítimas",
    ("evento", "detalle"): "eventos de login sin detalle",
    ("alerta", "patron_id"): "alertas generadas por un modelo, no por una regla",
    ("alerta", "analista_id"): "alertas sin revisar",
    ("alerta", "fecha_revision"): "alertas sin revisar",
    ("alerta", "veredicto"): "alertas sin revisar",
    ("alerta", "comentario"): "alertas sin comentario",
}


def get_client():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE)


def valores_nulos(client, tabla):
    columnas = client.query(f"DESCRIBE TABLE {tabla}").result_rows
    hallazgos = []
    for nombre_col, tipo_col, *_ in columnas:
        if "Nullable" in tipo_col: 
            n = client.query(f"SELECT count() FROM {tabla} WHERE {nombre_col} IS NULL").result_rows[0][0]
            if n > 0:
                hallazgos.append((nombre_col, "NULL", n))
        elif "String" in tipo_col: 
            n = client.query(f"SELECT count() FROM {tabla} WHERE {nombre_col} = ''").result_rows[0][0]
            if n > 0:
                hallazgos.append((nombre_col, "vacio", n))
    return hallazgos


def duplicados(client, tabla, columna_pk):
    q = f"""
        SELECT {columna_pk}, count() AS n
        FROM {tabla}
        GROUP BY {columna_pk}
        HAVING n > 1
    """
    return client.query(q).result_rows


def formatos_inconsistentes(client):
    resultados = []

    # emails que no tienen pinta de email (clientes y analistas)
    for tabla in ["cliente", "analista"]:
        n = client.query(
            f"SELECT count() FROM {tabla} WHERE NOT match(email, '^[^@]+@[^@]+\\.[^@]+$')"
        ).result_rows[0][0]
        resultados.append((f"{tabla}.email con formato invalido", n))

    # importes que no deberian ser 0 o negativos
    n = client.query("SELECT count() FROM transaccion WHERE cantidad <= 0").result_rows[0][0]
    resultados.append(("transaccion.cantidad <= 0", n))

    n = client.query("SELECT count() FROM devolucion WHERE importe <= 0").result_rows[0][0]
    resultados.append(("devolucion.importe <= 0", n))

    # nota_riesgo deberia estar siempre entre 0 y 1
    n = client.query(
        "SELECT count() FROM alerta WHERE nota_riesgo < 0 OR nota_riesgo > 1"
    ).result_rows[0][0]
    resultados.append(("alerta.nota_riesgo fuera de [0,1]", n))

    # tarjetas caducadas al final del periodo de datos (no respecto a hoy:
    # con today() el resultado cambiaria segun el dia en que se ejecute)
    n = client.query(
        "SELECT count() FROM metodo_pago "
        "WHERE fecha_expiracion < (SELECT toDate(max(timestamp)) FROM transaccion)"
    ).result_rows[0][0]
    resultados.append(("metodo_pago caducados al final del periodo", n))

    # coherencia entre tablas: fechas y etiquetas
    consultas = [
        ("transacciones con tarjeta ya caducada en esa fecha",
         "SELECT count() FROM transaccion t JOIN metodo_pago m ON t.metodo_id = m.metodo_id "
         "WHERE toDate(t.timestamp) > m.fecha_expiracion"),
        ("transacciones anteriores al alta del cliente",
         "SELECT count() FROM transaccion t JOIN metodo_pago m ON t.metodo_id = m.metodo_id "
         "JOIN cliente c ON m.cliente_id = c.cliente_id WHERE t.timestamp < c.fecha_alta"),
        ("sesiones que empiezan despues de su pago",
         "SELECT count() FROM transaccion t JOIN sesion s ON t.sesion_id = s.sesion_id "
         "WHERE s.timestamp > t.timestamp"),
        ("devoluciones mayores que la compra",
         "SELECT count() FROM devolucion d JOIN transaccion t ON d.transaccion_id = t.transaccion_id "
         "WHERE d.importe > t.cantidad"),
        ("devoluciones anteriores a la compra",
         "SELECT count() FROM devolucion d JOIN transaccion t ON d.transaccion_id = t.transaccion_id "
         "WHERE d.fecha < t.timestamp"),
        ("fraude sin tipo_fraude / legitima con tipo_fraude",
         "SELECT countIf(es_fraude = 1 AND tipo_fraude IS NULL) + countIf(es_fraude = 0 AND tipo_fraude IS NOT NULL) "
         "FROM transaccion"),
        ("alertas revisadas antes de generarse",
         "SELECT count() FROM alerta WHERE fecha_revision IS NOT NULL AND fecha_revision < fecha"),
        ("alertas con veredicto pero sin analista",
         "SELECT count() FROM alerta WHERE veredicto IS NOT NULL AND analista_id IS NULL"),
        ("alertas revisadas antes del alta del analista",
         "SELECT count() FROM alerta a JOIN analista n ON a.analista_id = n.analista_id "
         "WHERE a.fecha_revision < n.fecha_alta"),
    ]
    for descripcion, q in consultas:
        resultados.append((descripcion, client.query(q).result_rows[0][0]))

    return resultados


def huerfanos(client):
    # (nombre, tabla_hija, col_fk, tabla_padre, col_pk, fk_nullable)
    checks = [
        ("metodo_pago.cliente_id -> cliente", "metodo_pago", "cliente_id", "cliente", "cliente_id", False),
        ("canal_pago.comercio_id -> comercio", "canal_pago", "comercio_id", "comercio", "comercio_id", False),
        ("cliente_dispositivo.cliente_id -> cliente", "cliente_dispositivo", "cliente_id", "cliente", "cliente_id", False),
        ("cliente_dispositivo.dispositivo_id -> dispositivo", "cliente_dispositivo", "dispositivo_id", "dispositivo", "dispositivo_id", False),
        ("sesion.cliente_id -> cliente", "sesion", "cliente_id", "cliente", "cliente_id", False),
        ("sesion.dispositivo_id -> dispositivo", "sesion", "dispositivo_id", "dispositivo", "dispositivo_id", False),
        ("evento.sesion_id -> sesion", "evento", "sesion_id", "sesion", "sesion_id", False),
        ("transaccion.metodo_id -> metodo_pago", "transaccion", "metodo_id", "metodo_pago", "metodo_id", False),
        ("transaccion.canal_id -> canal_pago", "transaccion", "canal_id", "canal_pago", "canal_id", False),
        ("transaccion.dispositivo_id -> dispositivo", "transaccion", "dispositivo_id", "dispositivo", "dispositivo_id", True),
        ("transaccion.sesion_id -> sesion", "transaccion", "sesion_id", "sesion", "sesion_id", True),
        ("devolucion.transaccion_id -> transaccion", "devolucion", "transaccion_id", "transaccion", "transaccion_id", False),
        ("alerta.transaccion_id -> transaccion", "alerta", "transaccion_id", "transaccion", "transaccion_id", False),
        ("alerta.patron_id -> patron", "alerta", "patron_id", "patron", "patron_id", True),
        ("alerta.analista_id -> analista", "alerta", "analista_id", "analista", "analista_id", True),
    ]
    resultados = []
    for nombre, tabla_hija, col_fk, tabla_padre, col_pk, nullable in checks:
        # en las FK Nullable, un NULL no es un huerfano (pago con datafono, alerta sin revisar...)
        filtro_null = f"{col_fk} IS NOT NULL AND " if nullable else ""
        q = (f"SELECT count() FROM {tabla_hija} "
             f"WHERE {filtro_null}{col_fk} NOT IN (SELECT {col_pk} FROM {tabla_padre})")
        n = client.query(q).result_rows[0][0]
        resultados.append((nombre, n))
    return resultados


def fechas_futuras(client):
    checks = [
        ("transaccion", "timestamp"),
        ("sesion", "timestamp"),
        ("evento", "timestamp"),
        ("alerta", "fecha"),
        ("alerta", "fecha_revision"),
        ("devolucion", "fecha"),
        ("cliente", "fecha_alta"),
        ("analista", "fecha_alta"),
    ]
    resultados = []
    for tabla, columna in checks:
        n = client.query(f"SELECT count() FROM {tabla} WHERE {columna} > now()").result_rows[0][0]
        resultados.append((f"{tabla}.{columna} con fecha futura", n))
    return resultados


def importes_atipicos(client):
    media, desviacion = client.query(
        "SELECT avg(cantidad), stddevPop(cantidad) FROM transaccion"
    ).result_rows[0]

    limite = media + 3 * desviacion
    n = client.query(f"SELECT count() FROM transaccion WHERE cantidad > {limite}").result_rows[0][0]

    return media, desviacion, limite, n


if __name__ == "__main__":
    CARPETA_RESULTADOS.mkdir(exist_ok=True)
    f_out = CARPETA_RESULTADOS / f"informe_limpieza_{datetime.now():%Y%m%d_%H%M}.txt"
    with open(f_out, "w", encoding="utf-8") as f:
        def log(msg=""):
            print(msg)
            f.write(msg + "\n")

        client = get_client()

        log(" TABLAS ")
        for t in TABLAS:
            n = client.query(f"SELECT count() FROM {t}").result_rows[0][0]
            log(f"{t}: {n:,} filas")

        log("\n NULOS ")
        for t in TABLAS:
            res = valores_nulos(client, t)
            if res:
                log(f"\n{t}:")
                for col, tipo, cant in res:
                    motivo = ESPERADOS.get((t, col))
                    estado = f"ok ({motivo})" if motivo else "REVISAR"
                    log(f"  - {col} ({tipo}): {cant} -> {estado}")
            else:
                log(f"{t}: limpio")

        log("\n DUPLICADOS PK ")
        for t, pk in PK_POR_TABLA.items():
            dups = duplicados(client, t, pk)
            if dups:
                log(f"{t}: {len(dups)} valores duplicados en {pk} -> REVISAR")
                for row in dups[:3]:
                    log(f"   {row}")
            else:
                log(f"{t}: OK")

        log("\n FORMATOS Y COHERENCIA ")
        for desc, cant in formatos_inconsistentes(client):
            st = "OK" if cant == 0 else f"REVISAR ({cant})"
            log(f"  {desc}: {st}")

        log("\n HUÉRFANOS ")
        for desc, cant in huerfanos(client):
            st = "OK" if cant == 0 else f"REVISAR ({cant} huérfanas)"
            log(f"  {desc}: {st}")

        log("\n FECHAS FUTURAS ")
        for desc, cant in fechas_futuras(client):
            st = "OK" if cant == 0 else f"REVISAR ({cant})"
            log(f"  {desc}: {st}")

        log("\n OUTLIERS IMPORTES ")
        media, desv, limite, n = importes_atipicos(client)
        log(f"  Media: {media:.2f}€ | Desv: {desv:.2f}€ | Umbral: {limite:.2f}€")
        log(f"  Encima del umbral: {n} transacciones")

    print(f"\nGuardado en {f_out}")