"""
Cuentas de la web por comando: lo que hacemos nosotros (el proveedor) al instalar Centinela en una entidad,
o para arreglos. Lo normal después es todo desde la web: el administrador da las cuentas de su equipo en la
pestaña Cuentas y los comercios se registran solos (web/cuentas.py). Desde la raíz del proyecto:

    python -m web.usuarios crear jefa@banco.com "Ana López" administrador     # al instalar: el administrador
    python -m web.usuarios demo                  # una cuenta de cada tipo para probar (enseña las contraseñas)
    python -m web.usuarios listar
    python -m web.usuarios contrasena jefa@banco.com      # una contraseña provisional nueva
    python -m web.usuarios desactivar jefa@banco.com

Las cuentas que se crean aquí tienen una contraseña provisional (se enseña una vez): al entrar por primera
vez, la web pide cambiarla. Las contraseñas solo se guardan cifradas.
"""
import argparse
import sys
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ordenes = parser.add_subparsers(dest='orden', required=True)
    crear = ordenes.add_parser('crear', help='da de alta una cuenta con una contraseña provisional')
    crear.add_argument('email')
    crear.add_argument('nombre')
    crear.add_argument('rol', choices=ROLES)
    crear.add_argument('--comercio', type=int, default=0, help='comercio_id (obligatorio para un comercio)')
    ordenes.add_parser('demo', help='admin@, analista@ y comercio@centinela.demo, con contraseñas ya definitivas')
    ordenes.add_parser('listar', help='las cuentas y su estado')
    for orden, ayuda in [('contrasena', 'le da una contraseña provisional nueva'), ('desactivar', 'quita el acceso')]:
        ordenes.add_parser(orden, help=ayuda).add_argument('email')
    args = parser.parse_args()
    almacen = Almacen(cliente)
    almacen.preparar()

    if args.orden == 'crear':
        if args.rol == 'comercio':
            if not cliente().query('SELECT count() FROM comercio WHERE comercio_id = {c:UInt32}',
                                   parameters={'c': args.comercio}).result_rows[0][0]:
                raise SystemExit(f'No existe el comercio {args.comercio}: mira los comercio_id en la tabla comercio')
        if almacen.ficha(args.email) is not None:
            raise SystemExit(f'Ya hay una cuenta con {args.email} (para otra contraseña: contrasena {args.email})')
        contrasena = contrasena_provisional()
        almacen.guardar_usuario(args.email, args.nombre, args.rol, args.comercio, contrasena, provisional=True,
                                revisada_por='instalación')
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
