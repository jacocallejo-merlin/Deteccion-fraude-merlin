# Convierte las fechas de las transacciones brutas, que llegan como texto en varios formatos y zonas horarias, a DateTime en y las pasa a transaccion, aparta en transaccion_cuarentena las que no se pueden leer y comprueba que no se pierda ninguna fila.
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import clickhouse_connect
from config import HOST, PORT, USER, PASSWORD, DATABASE

# (nombre, patrón que reconoce el formato, cómo se convierte). Se prueba en orden.
FORMATOS_FECHA = [
    ("aaaa-mm-dd hh:mm:ss", r"^\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}$",
     "parseDateTimeOrNull(timestamp, '%Y-%m-%d %H:%i:%s')"),
    ("dd/mm/aaaa hh:mm:ss", r"^\\d{2}/\\d{2}/\\d{4} \\d{2}:\\d{2}:\\d{2}$",
     "parseDateTimeOrNull(timestamp, '%d/%m/%Y %H:%i:%s', )"),
    ("ISO con desfase horario", r"^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}[+-]\\d{2}:\\d{2}$",
     "parseDateTimeBestEffortOrNull(timestamp, )"),
]

FECHA = "multiIf(" + ", ".join(f"match(timestamp, '{patron}'), {conversion}"
                               for _, patron, conversion in FORMATOS_FECHA) + ", NULL)"

COLUMNAS = ["transaccion_id", "metodo_id", "canal_id", "dispositivo_id", "sesion_id", "timestamp",
            "cantidad", "moneda", "tipo_operacion", "metodo_autenticacion", "estado", "es_fraude",
            "tipo_fraude", "fecha_confirmacion"]


def get_client():
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                         database=DATABASE)


def main():
    client = get_client()
    n_brutas = client.command("SELECT count() FROM transaccion_bruta")
    print(f"transaccion_bruta: {n_brutas:,} filas")

    print("\nFormatos de fecha encontrados:")
    for nombre, patron, _ in FORMATOS_FECHA:
        n = client.command(f"SELECT count() FROM transaccion_bruta WHERE match(timestamp, '{patron}')")
        print(f"  {nombre:<28} {n:>9,}")

    client.command("TRUNCATE TABLE transaccion")
    client.command("TRUNCATE TABLE transaccion_cuarentena")
    # la fecha convertida se llama fecha: con el alias "timestamp" taparía la columna de texto
    convertidas = f"(SELECT *, {FECHA} AS fecha FROM transaccion_bruta)"
    client.command(f"""
        INSERT INTO transaccion ({", ".join(COLUMNAS)})
        SELECT transaccion_id, metodo_id, canal_id, dispositivo_id, sesion_id,
               assumeNotNull(fecha), cantidad, moneda, tipo_operacion,
               metodo_autenticacion, estado, es_fraude, tipo_fraude, fecha_confirmacion
        FROM {convertidas} WHERE fecha IS NOT NULL""")
    client.command(f"""
        INSERT INTO transaccion_cuarentena
        SELECT {", ".join(COLUMNAS)}, 'fecha ilegible o imposible' AS motivo
        FROM {convertidas} WHERE fecha IS NULL""")

    n_ok = client.command("SELECT count() FROM transaccion")
    n_cuarentena = client.command("SELECT count() FROM transaccion_cuarentena")
    print(f"\nCargadas en transaccion:       {n_ok:>9,}")
    print(f"En transaccion_cuarentena:     {n_cuarentena:>9,}  (fecha ilegible; revisar a mano)")
    if n_cuarentena:
        ejemplos = client.query("SELECT DISTINCT timestamp FROM transaccion_cuarentena LIMIT 5").result_rows
        print(f"  Ejemplos: {[e[0] for e in ejemplos]}")
        n_fraude = client.command("SELECT countIf(es_fraude = 1) FROM transaccion_cuarentena")
        print(f"  De ellas son fraude (solo lo sabe el simulador): {n_fraude}")
    if n_ok + n_cuarentena != n_brutas:
        sys.exit(f"ERROR: se han perdido filas ({n_brutas:,} brutas, {n_ok + n_cuarentena:,} procesadas)")
    print("OK: todas las filas brutas están cargadas o en cuarentena")


if __name__ == "__main__":
    main()
