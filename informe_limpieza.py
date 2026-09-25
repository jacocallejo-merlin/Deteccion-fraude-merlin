import clickhouse_connect
from datetime import datetime

HOST = "localhost"
PORT = 8123
USER = "default"
PASSWORD = "password"
DATABASE = "fraude_pagos"

TABLAS = ["cliente", "comercio", "dispositivo", "patron", "metodo_pago", "canal_pago", "cliente_dispositivo", "sesion", "evento", "transaccion", "devolucion", "alerta"]

PK_POR_TABLA = {
    "cliente": "cliente_id", "comercio": "comercio_id", "dispositivo": "dispositivo_id",
    "patron": "patron_id", "metodo_pago": "metodo_id", "canal_pago": "canal_id",
    "sesion": "sesion_id", "evento": "evento_id", "transaccion": "transaccion_id",
    "devolucion": "devolucion_id", "alerta": "alerta_id",}

def get_client():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD, database=DATABASE)

def valores_nulos(client, tabla):
    columnas = client.query(f"DESCRIBE TABLE {tabla}").result_rows
    hallazgos = []
    for nombre_col, tipo_col, *_ in columnas:
        if tipo_col.startswith("Nullable"):
            n = client.query(f"SELECT count() FROM {tabla} WHERE {nombre_col} IS NULL").result_rows[0][0]
            if n > 0:
                hallazgos.append((nombre_col, "NULL", n))
        elif tipo_col == "String":
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
 
    # emails que no tienen pinta de email
    n = client.query(
        "SELECT count() FROM cliente WHERE NOT match(email, '^[^@]+@[^@]+\\.[^@]+$')"
    ).result_rows[0][0]
    resultados.append(("cliente.email con formato invalido", n))
 
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
 
    # tarjetas con fecha de expiracion ya paso
    n = client.query(
        "SELECT count() FROM metodo_pago WHERE fecha_expiracion < today()"
    ).result_rows[0][0]
    resultados.append(("metodo_pago ya caducados", n))
 
    return resultados


def huerfanos(client):
    checks = [
        ("transaccion.metodo_id -> metodo_pago", "transaccion", "metodo_id", "metodo_pago", "metodo_id"),
        ("transaccion.sesion_id -> sesion", "transaccion", "sesion_id", "sesion", "sesion_id"),
        ("transaccion.canal_id -> canal_pago", "transaccion", "canal_id", "canal_pago", "canal_id"),
        ("sesion.cliente_id -> cliente", "sesion", "cliente_id", "cliente", "cliente_id"),
        ("alerta.transaccion_id -> transaccion", "alerta", "transaccion_id", "transaccion", "transaccion_id"),
        ("alerta.patron_id -> patron", "alerta", "patron_id", "patron", "patron_id"),
        ("devolucion.transaccion_id -> transaccion", "devolucion", "transaccion_id", "transaccion", "transaccion_id"),
    ]
    resultados = []
    for nombre, tabla_hija, col_fk, tabla_padre, col_pk in checks:
        q = f"SELECT count() FROM {tabla_hija} WHERE {col_fk} NOT IN (SELECT {col_pk} FROM {tabla_padre})"
        n = client.query(q).result_rows[0][0]
        resultados.append((nombre, n))
    return resultados

 
def fechas_futuras(client):
    checks = [
        ("transaccion", "timestamp"),
        ("sesion", "timestamp"),
        ("evento", "timestamp"),
        ("alerta", "fecha"),
        ("devolucion", "fecha"),
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
    nombre_archivo = f"informe_limpieza_{datetime.now():%Y%m%d_%H%M}.txt"

    with open(nombre_archivo, "w", encoding="utf-8") as archivo:
        def log(mensaje):
            print(mensaje)
            archivo.write(mensaje + "\n")
            archivo.flush()

        client = get_client()

        for tabla in TABLAS:
            n_filas = client.query(f"SELECT count() FROM {tabla}").result_rows[0][0]
            log(f"{tabla}: {n_filas:,} filas")

        log("\nValores nulos/vacios")
        for tabla in TABLAS:
            hallazgos = valores_nulos(client, tabla)
            if hallazgos:
                log(f"\n{tabla}:")
                for columna, tipo, cantidad in hallazgos:
                    log(f"  {columna}: {cantidad} valores {tipo}")
            else:
                log(f"{tabla}: sin nulos ni vacios")

        log("\nDuplicados por clave primaria")
        for tabla, pk in PK_POR_TABLA.items():
            dups = duplicados(client, tabla, pk)
            if dups:
                log(f"\n{tabla}: {len(dups)} valores de {pk} duplicados")
                for fila in dups[:5]:
                    log(f"{fila}")
            else:
                log(f"{tabla}: sin duplicados")

        log("\nFormatos inconsistentes")
        for descripcion, cantidad in formatos_inconsistentes(client):
            log(f"{descripcion}: {cantidad}")

        log("\nIntegridad referencial")
        for descripcion, cantidad in huerfanos(client):
            estado = "OK" if cantidad == 0 else f"!!!!PROBLEMA: {cantidad} filas huerfanas"
            log(f"  {descripcion}: {estado}")

        log("\n Fechas fuera de rango (futuras) ")
        for descripcion, cantidad in fechas_futuras(client):
            estado = "OK" if cantidad == 0 else f"!!!!PROBLEMA: {cantidad} filas con fecha futura"
            log(f"  {descripcion}: {estado}")

        log("\nImportes atipicos (outliers)")
        media, desviacion, limite, n = importes_atipicos(client)
        log(f"  Media de importe: {media:.2f}€")
        log(f"  Desviacion tipica: {desviacion:.2f}€")
        log(f"  Umbral : {limite:.2f}€")
        log(f"  Transacciones por encima del umbral: {n}")

    print(f"\nInforme guardado: {nombre_archivo}")