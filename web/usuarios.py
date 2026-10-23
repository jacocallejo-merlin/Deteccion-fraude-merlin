"""
Cuentas de la web por comando: lo que hacemos nosotros (el proveedor) al instalar Centinela en una entidad,
o para arreglos. Lo normal después es todo desde la web: el administrador da las cuentas de su equipo en la
pestaña Cuentas y los comercios se registran solos (web/cuentas.py). Desde la raíz del proyecto:

    python -m web.usuarios crear jefa@banco.com "Ana López" administrador     # al instalar: el administrador
    python -m web.usuarios crear ana@x.com "Ana" analista --definitiva  # sin cambio obligatorio (web publicada)
    python -m web.usuarios demo                  # una cuenta de cada tipo para probar (enseña las contraseñas)
    python -m web.usuarios listar
    python -m web.usuarios migrar                 # una vez: copia las cuentas que había en ClickHouse a CUENTAS_URL
    python -m web.usuarios contrasena jefa@banco.com      # una contraseña provisional nueva
    python -m web.usuarios desactivar jefa@banco.com

Las cuentas que se crean aquí tienen una contraseña provisional (se enseña una vez): al entrar por primera
vez, la web pide cambiarla. Las contraseñas solo se guardan cifradas.
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import clickhouse_connect

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from config import DATABASE, HOST, PASSWORD, PORT, USER
from web.sesiones import ROLES, Almacen, contrasena_provisional

_cliente = None


def cliente():
    global _cliente
    if _cliente is None:
        _cliente = clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD,
                                                 database=DATABASE)
    return _cliente


def epoca(fecha):
    """ClickHouse devuelve las fechas en UTC sin decirlo."""
    if isinstance(fecha, datetime):
        return int(fecha.replace(tzinfo=timezone.utc).timestamp()) if fecha.tzinfo is None else int(fecha.timestamp())
    return int(fecha or 0)


def migrar(almacen):
    """Las cuentas que guardaban las versiones anteriores en ClickHouse (tablas usuarios, ajustes_usuario y empresa),
    a la base de datos de cuentas. No pisa las que ya estén allí con el mismo correo (o empresa)."""
    ch = cliente()
    print(f'De ClickHouse ({DATABASE}) a {almacen.bd}')
    hechas = {'cuentas': 0, 'ajustes': 0, 'empresas': 0}
    if ch.command('EXISTS TABLE usuarios'):
        columnas = {c for (c, *_) in ch.query('DESCRIBE usuarios').result_rows}
        for u in ch.query('SELECT * FROM usuarios FINAL').named_results():
            if almacen.ficha(u['email']) is not None or u['email'].endswith('@invitado.centinela'):
                continue
            almacen._escribir({
                'email': u['email'], 'nombre': u['nombre'], 'rol': u['rol'], 'comercio_id': u['comercio_id'],
                'sal': u['sal'], 'hash': u['hash'],
                'estado': u.get('estado') or ('activa' if u['activo'] else 'desactivada'),
                'telefono': u.get('telefono', ''), 'nota': u.get('nota', ''),
                'creada': epoca(u.get('creada') or u.get('actualizado')), 'revisada_por': u.get('revisada_por', ''),
                'cambiar_contrasena': u.get('cambiar_contrasena', 0) if 'cambiar_contrasena' in columnas else 0})
            hechas['cuentas'] += 1
    if ch.command('EXISTS TABLE ajustes_usuario'):
        for email, clave, valor in ch.query('SELECT email, clave, valor FROM ajustes_usuario FINAL').result_rows:
            if clave not in almacen.ajustes(email):
                almacen.guardar_ajustes(email, {clave: valor})
                hechas['ajustes'] += 1
    if ch.command('EXISTS TABLE empresa'):
        for e in ch.query('SELECT * FROM empresa FINAL').named_results():
            if almacen.empresa(e['comercio_id']) is None:
                almacen.guardar_empresa(e['comercio_id'], e, registrada=e['registrada'], configurada=e['configurada'])
                hechas['empresas'] += 1
    print(f'Copiadas: {hechas["cuentas"]} cuentas, {hechas["ajustes"]} ajustes de Mis avisos y '
          f'{hechas["empresas"]} empresas. Las de ClickHouse se pueden dejar: la web ya no las usa.')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ordenes = parser.add_subparsers(dest='orden', required=True)
    crear = ordenes.add_parser('crear', help='da de alta una cuenta con una contraseña provisional')
    crear.add_argument('email')
    crear.add_argument('nombre')
    crear.add_argument('rol', choices=ROLES)
    crear.add_argument('--comercio', type=int, default=0, help='comercio_id (obligatorio para un comercio)')
    crear.add_argument('--definitiva', action='store_true',
                       help='sin cambio obligatorio al entrar (para la web publicada, que no guarda lo que se cambia en ella)')
    ordenes.add_parser('demo', help='admin@, analista@ y comercio@centinela.demo, con contraseñas ya definitivas')
    ordenes.add_parser('listar', help='las cuentas y su estado')
    ordenes.add_parser('migrar', help='copia las cuentas, Mis avisos y empresas que había en ClickHouse '
                                      '(versiones anteriores) a la base de datos de cuentas')
    for orden, ayuda in [('contrasena', 'le da una contraseña provisional nueva'), ('desactivar', 'quita el acceso')]:
        ordenes.add_parser(orden, help=ayuda).add_argument('email')
    args = parser.parse_args()
    almacen = Almacen()           # la base de datos de cuentas (CUENTAS_URL o SQLite en local)
    almacen.preparar()

    if args.orden == 'crear':
        if args.rol == 'comercio':
            if not cliente().query('SELECT count() FROM comercio WHERE comercio_id = {c:UInt32}',
                                   parameters={'c': args.comercio}).result_rows[0][0]:
                raise SystemExit(f'No existe el comercio {args.comercio}: mira los comercio_id en la tabla comercio')
        if almacen.ficha(args.email) is not None:
            raise SystemExit(f'Ya hay una cuenta con {args.email} (para otra contraseña: contrasena {args.email})')
        contrasena = contrasena_provisional()
        almacen.guardar_usuario(args.email, args.nombre, args.rol, args.comercio, contrasena,
                                provisional=not args.definitiva, revisada_por='instalación')
        if args.definitiva:
            print(f'Cuenta lista: {args.email.lower()} ({args.rol}). Contraseña: {contrasena}')
        else:
            print(f'Cuenta lista: {args.email.lower()} ({args.rol}). Contraseña provisional: {contrasena}\n'
                  'Dásela a la persona: al entrar por primera vez tendrá que poner una suya.')
    elif args.orden == 'demo':
        comercio_id = cliente().query('SELECT min(comercio_id) FROM comercio').result_rows[0][0]
        for email, nombre, rol, comercio in [('admin@centinela.demo', 'Administrador de prueba', 'administrador', 0),
                                             ('analista@centinela.demo', 'Analista de prueba', 'analista', 0),
                                             ('comercio@centinela.demo', 'Comercio de prueba', 'comercio', comercio_id)]:
            contrasena = contrasena_provisional()
            almacen.guardar_usuario(email, nombre, rol, comercio, contrasena, revisada_por='demo')
            print(f'{rol:13} {email:26} contrasena: {contrasena}' + (f'   (comercio {comercio})' if comercio else ''))
    elif args.orden == 'migrar':
        migrar(almacen)
    elif args.orden == 'listar':
        filas = almacen.cuentas()
        for c in filas:
            estado = c['estado'] if c['estado'] == 'activa' else c['estado'].upper()
            print(f'{c["email"]:32} {c["nombre"][:24]:24} {c["rol"]:13} {c["comercio_id"] or "":>6}  {estado:12}'
                  f'{"  (contraseña provisional)" if c["cambiar_contrasena"] else ""}')
        print(f'{len(filas)} cuenta{"s" if len(filas) != 1 else ""}')
    else:
        u = almacen.ficha(args.email)
        if u is None:
            raise SystemExit(f'No existe la cuenta {args.email}')
        almacen.cerrar_otras(u['email'])
        if args.orden == 'contrasena':
            contrasena = contrasena_provisional()
            almacen.cambiar_contrasena(u['email'], contrasena, provisional=True)
            print(f'Contraseña provisional nueva de {u["email"]}: {contrasena} (cerradas sus sesiones abiertas)')
        else:
            almacen.cambiar_estado(u['email'], 'desactivada', revisada_por='comando')
            print(f'Cuenta {u["email"]} desactivada')


if __name__ == '__main__':
    main()
