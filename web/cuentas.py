"""
Cuentas desde la web (se guardan con web/sesiones.py), como en cualquier servicio de verdad:

  - Contraseña olvidada: se escribe el correo, llega un código de 6 cifras (15 minutos, 5 intentos) y con
    él se pone una contraseña nueva. Se cierra la sesión en los demás equipos.
  - Un comercio se registra él mismo: su empresa, su nombre, su correo y una contraseña. Le llega un código
    para confirmar que el correo es suyo y entra. Se crea como comercio nuevo (tabla comercio, con un número
    nuevo) y empieza de cero: verá sus pagos cuando la entidad empiece a enviarlos. Registrándose nunca se
    puede quedar con un comercio que ya existe (vería sus pagos); a esos les da la cuenta el administrador.
    La primera vez que entra configura su empresa (CIF, sector, ciudad, teléfono...).
  - El administrador de la entidad da las cuentas de su equipo (y de los comercios que ya existen) en la
    pestaña Cuentas, con una contraseña provisional que la persona tiene que cambiar al entrar.

Sin servidor de correo (SMTP_* en .env), los códigos no pueden llegar: si la web se usa en local
(localhost), se enseñan en pantalla para poder probarla; publicada, solo salen en el registro del servidor.
Los datos de cada empresa (CIF, teléfono...) van en la base de datos de la aplicación (tabla empresa,
web/bd_cuentas.py), que se guarda de verdad. En ClickHouse solo está su fila de la tabla comercio, que es la que
se cruza con los pagos: si falta (se rehicieron los datos, o la web publicada acaba de arrancar), la web la vuelve
a poner.
"""
import json
import os
import re
import time
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import notificar
from web.sesiones import (DOMINIO_INVITADO, cifrar, con_sesion, contrasena_provisional, contrasena_valida,
                          es_invitado, usuario_de)

CORREO = re.compile(r'^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$')
PRIMER_REGISTRADO = 100_000           # los comercios que se registran, a partir de aquí (no chocan con los de la entidad)
# Rutas sin sesión (las añade web/app.py a las públicas del middleware) y las solo de administradores
PUBLICAS = {'/api/recuperar', '/api/recuperar/confirmar', '/api/registro', '/api/registro/confirmar',
            '/api/invitado', '/api/invitado/comercios'}
DE_ADMINISTRADORES = ('/api/cuentas',)
CAMPOS_EMPRESA = ['nombre', 'cif', 'categoria', 'pais', 'ciudad', 'direccion', 'telefono', 'web']

# ----------------------------------------------------------------------------- formularios
class Correo(BaseModel):
    email: str = Field(max_length=200)


class NuevaContrasena(BaseModel):
    email: str = Field(max_length=200)
    codigo: str = Field(max_length=10)
    contrasena: str = Field(max_length=200)
    recordar: bool = False


class Registro(BaseModel):
    empresa: str = Field(min_length=2, max_length=120)
    nombre: str = Field(min_length=2, max_length=80)
    email: str = Field(max_length=200)
    contrasena: str = Field(max_length=200)


class Confirmacion(BaseModel):
    email: str = Field(max_length=200)
    codigo: str = Field(max_length=10)
    recordar: bool = False


class Empresa(BaseModel):
    """Obligatorios: nombre, NIF/CIF, sector, país, ciudad y teléfono. Opcionales: dirección y web."""
    nombre: str = Field(max_length=120)
    cif: str = Field(max_length=20)
    categoria: str = Field(max_length=40)
    pais: str = Field(max_length=40)
    ciudad: str = Field(max_length=60)
    direccion: str = Field('', max_length=200)
    telefono: str = Field(max_length=30)
    web: str = Field('', max_length=200)


LETRAS_DNI = 'TRWAGMYFPDXBNJZSQVHLCKE'


def nif_espanol_valido(nif):
    """NIF de persona (12345678Z), NIE (X1234567L) o CIF de empresa (B12345674), con su letra o dígito de control."""
    if re.fullmatch(r'\d{8}[A-Z]', nif):
        return nif[-1] == LETRAS_DNI[int(nif[:8]) % 23]
    if re.fullmatch(r'[XYZ]\d{7}[A-Z]', nif):
        return nif[-1] == LETRAS_DNI[int('XYZ'.index(nif[0]).__str__() + nif[1:8]) % 23]
    if re.fullmatch(r'[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]', nif):
        cifras = [int(c) for c in nif[1:8]]
        pares = sum(cifras[1::2])
        impares = sum(sum(divmod(2 * c, 10)) for c in cifras[0::2])
        control = (10 - (pares + impares) % 10) % 10
        return nif[-1] in (str(control), 'JABCDEFGHI'[control])
    return False


def validar_empresa(e):
    """Los datos de la empresa, limpios. Lanza un 422 que dice qué falta o qué está mal."""
    v = {k: getattr(e, k).strip() for k in CAMPOS_EMPRESA}
    nombres = {'nombre': 'el nombre comercial', 'cif': 'el NIF o CIF', 'categoria': 'el sector', 'pais': 'el país',
               'ciudad': 'la ciudad', 'telefono': 'el teléfono'}
    faltan = [texto for campo, texto in nombres.items() if not v[campo]]
    if faltan:
        raise HTTPException(422, 'Falta ' + ', '.join(faltan))
    if len(v['nombre']) < 2:
        raise HTTPException(422, 'El nombre comercial es demasiado corto')
    v['cif'] = re.sub(r'[\s.-]', '', v['cif']).upper()
    if v['pais'].lower() in ('españa', 'espana', 'spain', 'es'):
        if not nif_espanol_valido(v['cif']):
            raise HTTPException(422, 'Ese NIF o CIF no es válido (comprueba la letra o el último dígito)')
    elif not re.fullmatch(r'[A-Z0-9]{5,15}', v['cif']):
        raise HTTPException(422, 'Ese número de identificación fiscal no es válido')
    if len(re.sub(r'\D', '', v['telefono'])) < 9 or not re.fullmatch(r'\+?[\d\s()-]{9,25}', v['telefono']):
        raise HTTPException(422, 'Ese teléfono no es válido (al menos 9 cifras)')
    return v


class NuevaCuenta(BaseModel):
    """Sin contraseña, se genera una provisional (y se enseña al administrador para que se la dé)."""
    rol: Literal['administrador', 'analista', 'comercio'] = 'analista'
    email: str = Field(max_length=200)
    nombre: str = Field('', max_length=80)
    comercio_id: int = Field(0, ge=0)
    contrasena: Optional[str] = Field(None, max_length=200)


class Decision(BaseModel):
    email: str = Field(max_length=200)
    accion: Literal['desactivar', 'reactivar', 'contrasena_provisional']


def correo_valido(email):
    email = email.strip().lower()
    if not CORREO.match(email):
        raise HTTPException(422, f'Ese correo no es válido: {email}')
    return email


class Invitado(BaseModel):
    rol: Literal['administrador', 'analista', 'comercio']
    comercio_id: int = Field(0, ge=0)            # el invitado de comercio elige qué empresa ver


def invitados_permitidos(request):
    """Entrar como invitado, para enseñar o probar la web: en este equipo (localhost) o si se activa con
    MODO_INVITADO=1 (la web publicada para el tutor). En una instalación de verdad, apagado."""
    valor = os.environ.get('MODO_INVITADO', '').strip().lower()
    if valor in ('0', 'no', 'false'):
        return False
    return valor in ('1', 'si', 'sí', 'true') or request.url.hostname in ('localhost', '127.0.0.1', 'testserver')


def en_local(request):
    """¿Se puede enseñar el código en pantalla? Solo sin servidor de correo y abriendo la web en este equipo."""
    return notificar._correo_config(['x@x.x']) is None and request.url.hostname in ('localhost', '127.0.0.1', 'testserver')


def crear_router(almacen, nombre_comercio, cliente):
    """almacen: función que devuelve el Almacen (la base de datos de la aplicación). nombre_comercio(id): su
    nombre, o None si no existe. cliente: la conexión a ClickHouse (para la tabla comercio)."""
    router = APIRouter()
    preparada = []

    def preparar_empresas():
        """Los comercios registrados, de vuelta en la tabla comercio de ClickHouse si les falta su fila."""
        if not preparada:
            registradas = almacen().empresas_registradas()
            if registradas:
                ya = {c for (c,) in cliente().query('SELECT comercio_id FROM comercio').result_rows}
                faltan = [[e['comercio_id'], e['nombre'], e['categoria'], e['pais'], e['ciudad']]
                          for e in registradas if e['comercio_id'] not in ya]
                if faltan:
                    cliente().insert('comercio', faltan,
                                     column_names=['comercio_id', 'nombre', 'categoria_negocio', 'pais', 'ciudad'])
            preparada.append(True)

    def mandar_codigo(request, tareas, email, proposito, datos=''):
        """Crea el código y lo manda por correo. En local sin correo, lo devuelve para enseñarlo en pantalla."""
        codigo = almacen().nuevo_codigo(email, proposito, datos)
        asunto = {'recuperar': 'Tu código para cambiar la contraseña de Centinela',
                  'registro': 'Tu código para confirmar tu cuenta de Centinela'}[proposito]
        tareas.add_task(notificar.a_correo, email, asunto,
                        f'Tu código es {codigo}. Vale 15 minutos. Si no lo has pedido tú, no hagas nada.')
        if notificar._correo_config([email]) is None:
            print(f'Centinela: código de {proposito} para {email}: {codigo} (no hay servidor de correo)', flush=True)
        return {'codigo_demo': codigo} if en_local(request) else {}

    # ------------------------------------------------------------------ entrar como invitado
    @router.get('/api/invitado')
    def hay_invitados(request: Request):
        return {'disponible': invitados_permitidos(request)}

    @router.get('/api/invitado/comercios')
    def comercios_para_invitado(request: Request):
        """Las empresas que puede elegir el invitado de comercio (nombre, sector y ciudad), con cuántos pagos
        tiene cada una. Solo con el modo invitado activado: si no, la lista no es pública."""
        if not invitados_permitidos(request):
            raise HTTPException(404, 'Not Found')
        return list(cliente().query('''
            SELECT c.comercio_id AS id, c.nombre AS nombre, c.categoria_negocio AS categoria, c.ciudad AS ciudad,
                   ifNull(p.pagos, 0) AS pagos
            FROM comercio AS c
            LEFT JOIN (SELECT comercio_id, count() AS pagos FROM transacciones_enriquecidas GROUP BY comercio_id) AS p
                   ON p.comercio_id = c.comercio_id
            ORDER BY c.nombre''').named_results())

    @router.post('/api/invitado')
    def entrar_como_invitado(i: Invitado, request: Request):
        """Una cuenta de solo lectura por rol y, para comercios, una por empresa (así dos personas pueden mirar
        empresas distintas a la vez). Su contraseña es aleatoria y no se enseña: solo se entra por aquí."""
        if not invitados_permitidos(request):
            raise HTTPException(404, 'Not Found')
        if i.rol == 'comercio':
            nombre = nombre_comercio(i.comercio_id) if i.comercio_id else None
            if nombre is None:
                raise HTTPException(422, 'Elige la empresa que quieres ver')
            email, nombre = f'comercio-{i.comercio_id}{DOMINIO_INVITADO}', f'Invitado ({nombre})'
        else:
            email, nombre = f'{i.rol}{DOMINIO_INVITADO}', f'Invitado ({i.rol})'
        if almacen().usuario(email) is None:
            almacen().guardar_usuario(email, nombre, i.rol, i.comercio_id, contrasena_provisional() * 3,
                                      revisada_por='invitado')
        return con_sesion(JSONResponse({'rol': i.rol, 'invitado': True}), request, almacen(), email, False)

    # ------------------------------------------------------------------ contraseña olvidada
    @router.post('/api/recuperar')
    def recuperar(c: Correo, request: Request, tareas: BackgroundTasks):
        """Responde lo mismo exista o no la cuenta: así no se puede averiguar quién tiene cuenta."""
        email = c.email.strip().lower()
        time.sleep(0.3)
        if CORREO.match(email) and almacen().usuario(email) is not None:
            return {'ok': True, **mandar_codigo(request, tareas, email, 'recuperar')}
        return {'ok': True}

    @router.post('/api/recuperar/confirmar')
    def nueva_contrasena(n: NuevaContrasena, request: Request):
        email = n.email.strip().lower()
        contrasena_valida(n.contrasena)
        almacen().usar_codigo(email, 'recuperar', n.codigo)
        if almacen().usuario(email) is None:
            raise HTTPException(403, 'Esta cuenta está desactivada. Contacta con el administrador de tu entidad.')
        almacen().cambiar_contrasena(email, n.contrasena)
        almacen().cerrar_otras(email)                # quien conociera la antigua, fuera
        u = almacen().usuario(email)
        return con_sesion(JSONResponse({'nombre': u['nombre'], 'rol': u['rol']}), request, almacen(), email,
                          n.recordar)

    # ------------------------------------------------------------------ un comercio se registra
    @router.post('/api/registro')
    def registrar(r: Registro, request: Request, tareas: BackgroundTasks):
        email = correo_valido(r.email)
        contrasena_valida(r.contrasena)
        time.sleep(0.3)
        if almacen().ficha(email) is not None:
            raise HTTPException(409, 'Ya hay una cuenta con ese correo. Si no recuerdas la contraseña, usa '
                                     '"¿Has olvidado tu contraseña?"')
        sal, hash_ = cifrar(r.contrasena)            # hasta confirmar el correo, solo se guarda cifrada
        datos = json.dumps({'empresa': r.empresa.strip(), 'nombre': r.nombre.strip(), 'sal': sal, 'hash': hash_})
        return {'ok': True, **mandar_codigo(request, tareas, email, 'registro', datos)}

    @router.post('/api/registro/confirmar')
    def confirmar_registro(c: Confirmacion, request: Request):
        email = correo_valido(c.email)
        datos = json.loads(almacen().usar_codigo(email, 'registro', c.codigo))
        if almacen().ficha(email) is not None:
            raise HTTPException(409, 'Ya hay una cuenta con ese correo')
        preparar_empresas()
        en_clickhouse = cliente().query('SELECT max(comercio_id) FROM comercio').result_rows[0][0] or 0
        comercio_id = max(PRIMER_REGISTRADO - 1, en_clickhouse, almacen().ultimo_comercio_registrado()) + 1
        almacen().guardar_empresa(comercio_id, {'nombre': datos['empresa']}, registrada=True, configurada=False)
        cliente().insert('comercio', [[comercio_id, datos['empresa'], '', '', '']],
                         column_names=['comercio_id', 'nombre', 'categoria_negocio', 'pais', 'ciudad'])
        almacen().guardar_usuario(email, datos['nombre'], 'comercio', comercio_id, None,
                                  cifrada=(datos['sal'], datos['hash']), revisada_por='registro')
        return con_sesion(JSONResponse({'nombre': datos['nombre'], 'rol': 'comercio'}), request, almacen(), email,
                          c.recordar)

    # ------------------------------------------------------------------ Configura tu empresa (comercios)
    def mi_empresa(comercio_id):
        preparar_empresas()
        guardada = almacen().empresa(comercio_id)
        if guardada:
            return guardada
        # Un comercio que ya estaba en los datos de la entidad: lo que se sabe de él, sin configurar todavía
        base = list(cliente().query('''SELECT nombre, categoria_negocio AS categoria, pais, ciudad FROM comercio
                                       WHERE comercio_id = {c:UInt32}''', parameters={'c': comercio_id}).named_results())
        return {**{k: '' for k in CAMPOS_EMPRESA}, **(base[0] if base else {}), 'registrada': 0, 'configurada': 0}

    def solo_comercios(request):
        u = usuario_de(request)
        if u['rol'] != 'comercio':
            raise HTTPException(403, 'Esto es para las cuentas de comercio')
        return u

    @router.get('/api/empresa')
    def leer_empresa(request: Request):
        u = solo_comercios(request)
        e = mi_empresa(u['comercio_id'])
        sectores = [s for (s,) in cliente().query(
            "SELECT DISTINCT categoria_negocio FROM comercio WHERE categoria_negocio != '' ORDER BY 1").result_rows]
        pagos = cliente().query('SELECT count() FROM transacciones_enriquecidas WHERE comercio_id = {c:UInt32}',
                                parameters={'c': u['comercio_id']}).result_rows[0][0]
        return {**e, 'comercio_id': u['comercio_id'], 'configurada': bool(e['configurada']),
                'registrada': bool(e['registrada']), 'sectores': sectores, 'pagos': pagos}

    @router.post('/api/empresa')
    def guardar_empresa(e: Empresa, request: Request):
        u = solo_comercios(request)
        antes = mi_empresa(u['comercio_id'])
        valores = validar_empresa(e)
        almacen().guardar_empresa(u['comercio_id'], valores, registrada=antes['registrada'], configurada=True)
        # El nombre, el sector y la ciudad, también donde los leen las webs y el pipeline
        cliente().command('''ALTER TABLE comercio UPDATE nombre = {n:String}, categoria_negocio = {s:String},
                                 pais = {p:String}, ciudad = {ci:String} WHERE comercio_id = {c:UInt32}
                             SETTINGS mutations_sync = 1''',
                          parameters={'n': valores['nombre'], 's': valores['categoria'], 'p': valores['pais'],
                                      'ci': valores['ciudad'], 'c': u['comercio_id']})
        return leer_empresa(request)

    # ------------------------------------------------------------------ pestaña Cuentas (administradores)
    @router.get('/api/cuentas')
    def cuentas():
        nombres = {}
        filas = []
        for c in almacen().cuentas():
            if es_invitado(c):
                continue                          # las de invitado no son de nadie: no se gestionan
            if c['comercio_id'] and c['comercio_id'] not in nombres:
                nombres[c['comercio_id']] = nombre_comercio(c['comercio_id'])
            filas.append({**c, 'comercio': nombres.get(c['comercio_id'])})
        return {'cuentas': filas}

    @router.post('/api/cuentas/crear')
    def crear(n: NuevaCuenta, request: Request):
        yo = usuario_de(request)
        email = correo_valido(n.email)
        if almacen().ficha(email) is not None:
            raise HTTPException(409, f'Ya hay una cuenta con {email}')
        nombre = n.nombre.strip()
        if n.rol == 'comercio':
            comercio = nombre_comercio(n.comercio_id) if n.comercio_id else None
            if comercio is None:
                raise HTTPException(422, f'No hay ningún comercio con el número {n.comercio_id}')
            nombre = nombre or comercio
        if len(nombre) < 2:
            raise HTTPException(422, 'Pon el nombre de la persona')
        contrasena = contrasena_valida(n.contrasena) if n.contrasena else contrasena_provisional()
        almacen().guardar_usuario(email, nombre, n.rol, n.comercio_id, contrasena, provisional=True,
                                  revisada_por=yo['email'])
        return {'email': email, 'contrasena_provisional': contrasena}

    @router.post('/api/cuentas/estado')
    def decidir(d: Decision, request: Request):
        yo = usuario_de(request)
        email = d.email.strip().lower()
        if email == yo['email'] and d.accion != 'reactivar':
            raise HTTPException(422, 'Tu propia cuenta la cambias en Mi perfil')
        u = almacen().ficha(email)
        if u is None:
            raise HTTPException(404, 'No existe esa cuenta')
        if d.accion == 'contrasena_provisional':
            contrasena = contrasena_provisional()
            almacen().cambiar_contrasena(email, contrasena, provisional=True)
            almacen().cerrar_otras(email)
            return {'email': email, 'estado': u['estado'], 'contrasena_provisional': contrasena}
        u = almacen().cambiar_estado(email, 'desactivada' if d.accion == 'desactivar' else 'activa',
                                     revisada_por=yo['email'])
        if d.accion == 'desactivar':
            almacen().cerrar_otras(email)             # fuera ya, en todos sus equipos
        return {'email': email, 'estado': u['estado']}

    return router
