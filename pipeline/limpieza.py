import sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clickhouse_connect
from config import CONFIG, HOST, PORT, USER, PASSWORD, DATABASE

TABLAS = ["cliente", "comercio", "dispositivo", "patron", "analista", "metodo_pago", "canal_pago",
          "cliente_dispositivo", "sesion", "evento", "transaccion", "devolucion", "alerta"]

PK_POR_TABLA = {
    "cliente": "cliente_id", "comercio": "comercio_id", "dispositivo": "dispositivo_id",
    "patron": "patron_id", "analista": "analista_id", "metodo_pago": "metodo_id",
    "canal_pago": "canal_id", "sesion": "sesion_id", "evento": "evento_id",
    "transaccion": "transaccion_id", "devolucion": "devolucion_id", "alerta": "alerta_id",
    "cliente_dispositivo": "cliente_id, dispositivo_id",
}

# Columnas categóricas que comparten valores: la forma correcta de "España" se decide mirando
# las tres columnas de país a la vez, no cada una por separado.
DOMINIOS = {
    "pais": [("cliente", "pais"), ("comercio", "pais"), ("sesion", "ip_pais")],
    "estado_transaccion": [("transaccion", "estado")],
    "moneda": [("transaccion", "moneda")],
    "autenticacion": [("transaccion", "metodo_autenticacion")],
}

# Columnas sin valores repetidos: no hay forma "mayoritaria", se normalizan con una regla fija
EMAILS = [("cliente", "email"), ("analista", "email")]

AJUSTES_MUTACION = {"mutations_sync": 2}

MONEDA = "EUR"
TIPOS_CAMBIO = CONFIG["suciedad"]["tipos_cambio"]   # unidades de cada divisa por 1 EUR


def clave(valor):
    return valor.strip().lower()


def elegir_canonicos(conteos):
    """conteos: {valor: n_filas}. Agrupa los valores que solo difieren en mayúsculas o espacios
    y devuelve {variante: forma_correcta} para las variantes que hay que cambiar.
    La forma correcta es la más frecuente entre las que no tienen espacios sobrantes."""
    grupos = defaultdict(list)
    for valor, n in conteos.items():
        grupos[clave(valor)].append((valor, n))
    cambios = {}
    for variantes in grupos.values():
        if len(variantes) == 1 and variantes[0][0] == variantes[0][0].strip():
            continue
        sin_espacios = [v for v in variantes if v[0] == v[0].strip()] or \
                       [(v.strip(), n) for v, n in variantes]
        canonico = max(sin_espacios, key=lambda v: (v[1], v[0]))[0]
        for valor, _ in variantes:
            if valor != canonico:
                cambios[valor] = canonico
    return cambios


def get_client():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                         database=DATABASE)


def contar(client, sql):
    return client.query(sql).result_rows[0][0]


def quitar_duplicados(client):
    print("1. Filas duplicadas")
    for tabla in TABLAS:
        antes = contar(client, f"SELECT count() FROM {tabla}")
        if antes == 0:
            continue
        # elimina las filas idénticas en todas sus columnas
        client.command(f"OPTIMIZE TABLE {tabla} FINAL DEDUPLICATE")
        despues = contar(client, f"SELECT count() FROM {tabla}")
        pk = PK_POR_TABLA[tabla]
        conflictos = contar(client, f"SELECT count() FROM (SELECT {pk} FROM {tabla} "
                                    f"GROUP BY {pk} HAVING count() > 1)")
        nota = f" | {conflictos} ids repetidos con contenido distinto -> REVISAR" if conflictos else ""
        if antes != despues or conflictos:
            print(f"  {tabla}: {antes - despues:,} filas repetidas eliminadas{nota}")


def normalizar_categorias(client):
    print("2. Valores categóricos escritos de varias formas")
    for dominio, columnas in DOMINIOS.items():
        conteos = defaultdict(int)
        for tabla, col in columnas:
            for valor, n in client.query(f"SELECT {col}, count() FROM {tabla} "
                                         f"WHERE {col} IS NOT NULL GROUP BY {col}").result_rows:
                conteos[valor] += n
        cambios = elegir_canonicos(conteos)
        if not cambios:
            print(f"  {dominio}: sin cambios")
            continue
        for tabla, col in columnas:
            for variante, canonico in cambios.items():
                n = client.query(f"SELECT count() FROM {tabla} WHERE {col} = %(v)s",
                                 parameters={"v": variante}).result_rows[0][0]
                if n:
                    client.command(f"ALTER TABLE {tabla} UPDATE {col} = %(c)s WHERE {col} = %(v)s",
                                   parameters={"c": canonico, "v": variante}, settings=AJUSTES_MUTACION)
        n_filas = sum(conteos[v] for v in cambios)
        print(f"  {dominio}: {n_filas:,} filas corregidas en {len(cambios)} variantes "
              f"(p. ej. {next(iter(cambios.items()))!r})")


def convertir_divisas(client):
    print(f"3. Importes en otras divisas -> {MONEDA}")
    for moneda, n in client.query(f"SELECT moneda, count() FROM transaccion WHERE moneda != '{MONEDA}' "
                                  "GROUP BY moneda").result_rows:
        if moneda not in TIPOS_CAMBIO:
            print(f"  {moneda}: {n:,} transacciones SIN tipo de cambio en config.yaml -> REVISAR")
            continue
        client.command(f"ALTER TABLE transaccion UPDATE "
                       f"cantidad = toDecimal64(round(toFloat64(cantidad) / {TIPOS_CAMBIO[moneda]}, 2), 2), "
                       f"moneda = '{MONEDA}' WHERE moneda = '{moneda}'", settings=AJUSTES_MUTACION)
        print(f"  {moneda}: {n:,} transacciones convertidas (1 {MONEDA} = {TIPOS_CAMBIO[moneda]} {moneda})")


def normalizar_emails(client):
    print("4. Emails (minúsculas y sin espacios)")
    for tabla, col in EMAILS:
        n = contar(client, f"SELECT count() FROM {tabla} WHERE {col} != lowerUTF8(trimBoth({col}))")
        if n:
            client.command(f"ALTER TABLE {tabla} UPDATE {col} = lowerUTF8(trimBoth({col})) "
                           f"WHERE {col} != lowerUTF8(trimBoth({col}))", settings=AJUSTES_MUTACION)
        print(f"  {tabla}.{col}: {n:,} corregidos")


def verificar(client):
    """Comprueba que no queda nada por corregir. Devuelve el número de problemas."""
    problemas = 0
    for tabla in TABLAS:
        pk = PK_POR_TABLA[tabla]
        problemas += contar(client, f"SELECT count() FROM (SELECT {pk} FROM {tabla} "
                                    f"GROUP BY {pk} HAVING count() > 1)")
    for columnas in DOMINIOS.values():
        for tabla, col in columnas:
            problemas += contar(client, f"SELECT count() FROM {tabla} WHERE {col} != trimBoth({col})")
        conteos = defaultdict(int)
        for tabla, col in columnas:
            for valor, n in client.query(f"SELECT {col}, count() FROM {tabla} "
                                         f"WHERE {col} IS NOT NULL GROUP BY {col}").result_rows:
                conteos[valor] += n
        problemas += len(elegir_canonicos(conteos))
    for tabla, col in EMAILS:
        problemas += contar(client, f"SELECT count() FROM {tabla} WHERE {col} != lowerUTF8(trimBoth({col}))")
    problemas += contar(client, f"SELECT count() FROM transaccion WHERE moneda != '{MONEDA}'")
    return problemas


def main():
    client = get_client()
    quitar_duplicados(client)
    normalizar_categorias(client)
    convertir_divisas(client)
    normalizar_emails(client)
    problemas = verificar(client)
    print(f"\nVerificación tras la limpieza: {'OK' if problemas == 0 else f'{problemas} problemas pendientes'}")
    if problemas:
        sys.exit(1)


if __name__ == "__main__":
    main()
