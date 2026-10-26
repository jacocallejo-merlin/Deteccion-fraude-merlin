"""
Las revisiones (veredictos) que se dan en la web, guardadas dos veces: en revision_web de ClickHouse, que es la
que usan la web y el recalibrado semanal, y en la base de datos de cuentas (tabla revisiones, web/bd_cuentas.py),
que no se pierde. La web publicada vuelve a cargar ClickHouse desde una copia cada vez que arranca: sin esto, lo
revisado allí desaparecía. Al arrancar la web y antes de cada recalibrado (integrar_feedback.py) se ponen en
revision_web las que falten.

Cada sitio tiene su propia simulación (el ordenador de cada uno, la web publicada y el scoring de la noche, que
comparten la suya): el mismo transaccion_id no es el mismo pago en todos. Por eso cada revisión lleva la huella
de los datos en que se hizo (la base de datos y su primer día) y solo se recupera en esos datos.
"""
from datetime import datetime, timezone

HUELLAS = {}


def huella(client):
    """Qué datos son estos: la base de datos y el primer día de sus transacciones."""
    clave = id(client)
    if clave not in HUELLAS:
        primero = client.query('SELECT toString(min(toDate(timestamp))) FROM transaccion').result_rows[0][0]
        HUELLAS[clave] = f'{client.database}:{primero}'
    return HUELLAS[clave]


def en_utc(fecha):
    """ClickHouse devuelve las fechas en UTC sin decirlo."""
    return fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha


def sincronizar(almacen, client):
    """Pone en revision_web las revisiones de estos datos guardadas en la base de cuentas que no estén.
    Devuelve cuántas ha puesto. Si falla algo, avisa y sigue: nunca impide arrancar ni recalibrar."""
    try:
        from integrar_feedback import crear_tabla_revision_web
        crear_tabla_revision_web(client)
        mias = [r for r in almacen.revisiones() if r['datos'] == huella(client)]
        if not mias:
            return 0
        ya = {(t, int(en_utc(f).timestamp())) for t, f in client.query(
            'SELECT transaccion_id, fecha_real FROM revision_web').result_rows}
        faltan = [r for r in mias if (r['transaccion_id'], r['fecha_real']) not in ya]
        if faltan:
            client.insert('revision_web', [
                [r['transaccion_id'], r['veredicto'], r['comentario'], r['usuario'], r['comercio'],
                 datetime.fromisoformat(r['fecha_revision']), datetime.fromtimestamp(r['fecha_real'], timezone.utc)]
                for r in faltan],
                column_names=['transaccion_id', 'veredicto', 'comentario', 'usuario', 'comercio', 'fecha_revision',
                              'fecha_real'])
            print(f'Revisiones de la web recuperadas de la base de cuentas: {len(faltan)}')
        return len(faltan)
    except Exception as e:
        print(f'Revisiones de la web: no se han podido recuperar ({str(e)[:150]})')
        return 0


def guardar(almacen, client, filas, email):
    """Guarda las revisiones nuevas en los dos sitios. filas: [transaccion_id, veredicto, comentario, usuario,
    comercio, fecha_revision] como las inserta la web."""
    fecha_real = datetime.now(timezone.utc).replace(microsecond=0)
    client.insert('revision_web', [[*f, fecha_real] for f in filas],
                  column_names=['transaccion_id', 'veredicto', 'comentario', 'usuario', 'comercio', 'fecha_revision',
                                'fecha_real'])
    datos = huella(client)
    for t, veredicto, comentario, usuario, comercio, fecha_revision in filas:
        almacen.guardar_revision({'transaccion_id': int(t), 'veredicto': veredicto, 'comentario': comentario,
                                  'usuario': usuario, 'email': email, 'comercio': int(comercio),
                                  'fecha_revision': fecha_revision.strftime('%Y-%m-%d %H:%M:%S'),
                                  'fecha_real': int(fecha_real.timestamp()), 'datos': datos})
