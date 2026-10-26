"""
Inicio de sesión de las dos webs (casos y modelos).

Centinela se vende a una entidad de pagos (un banco o una pasarela de pago). Una sola pantalla de entrada
(correo y contraseña): el rol lo dice la cuenta, no hay que elegirlo. Tres tipos de cuenta:
  - administrador: de la entidad. Lo creamos nosotros al instalar (python -m web.usuarios), con una
    contraseña provisional. Da las cuentas de su equipo (pestaña Cuentas), ve todo y la web de modelos.
  - analista: el equipo de fraude. Se la crea el administrador con una contraseña provisional. Ve todos
    los comercios.
  - comercio: se registra él mismo (su empresa, su correo y una contraseña, confirmando un código que le
    llega al correo) o se la da el administrador. Solo ve sus pagos, sin la web de modelos ni el modo
    demostración. La primera vez que entra configura su empresa (web/cuentas.py).
Una contraseña provisional hay que cambiarla al entrar: hasta entonces la web no enseña nada más. Si se
olvida, se pide un código de 6 cifras por correo y con él se pone una nueva.

La seguridad está en el servidor, en un middleware que revisa cada petición antes de que llegue a las
rutas: sin sesión, /api/... responde 401 y las páginas mandan a /login; a un comercio se le cambia el
comercio que pida la página por el suyo (y se le quita el modo demostración) y en las revisiones el
nombre de quien revisa sale de su cuenta. Así las rutas de web/app.py no tienen que saber nada de esto.

Las cuentas y sesiones van en la base de datos de la aplicación (web/bd_cuentas.py: PostgreSQL en la nube con
CUENTAS_URL, compartida por todos, o SQLite en local), no en ClickHouse, que es para el análisis. Contraseñas con
PBKDF2-SHA256 (biblioteca estándar). Cerrar sesión la invalida de verdad, y la web de modelos, en otro puerto, puede
comprobarla (el navegador manda la misma cookie a los dos puertos de la misma máquina).
"""
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from web.bd_cuentas import BaseCuentas, ahora

COOKIE = 'centinela_sesion'
DURACION_RECORDAR = timedelta(days=30)       # "Mantener la sesión iniciada"
DURACION_NORMAL = timedelta(hours=12)        # sin marcarla: hasta cerrar el navegador (y como mucho 12 h)
DURACION_CODIGO = timedelta(minutes=15)      # los códigos de 6 cifras que llegan por correo
INTENTOS_CODIGO = 5
ITERACIONES = 200_000
CACHE_SEGUNDOS = 60                          # una sesión ya comprobada no se vuelve a pedir a ClickHouse en 1 min
MINIMO_CONTRASENA = 8
ROLES = ('administrador', 'analista', 'comercio')
DE_LA_ENTIDAD = ('administrador', 'analista')   # ven todos los comercios y reciben los avisos del sistema
ESTADOS = ('activa', 'desactivada')
ESTATICOS_SESION = Path(__file__).resolve().parent / 'static'
LOGIN_HTML = ESTATICOS_SESION / 'login.html'
# Con una contraseña provisional solo se puede cambiarla (y ver quién eres o salir)
SIN_CAMBIAR_CONTRASENA = {'/api/sesion', '/api/logout', '/api/perfil/contrasena'}
# Las cuentas de invitado (para enseñar la web sin contraseñas, ver web/cuentas.py) solo pueden mirar
DOMINIO_INVITADO = '@invitado.centinela'

COLUMNAS = ['email', 'nombre', 'rol', 'comercio_id', 'sal', 'hash', 'estado', 'telefono', 'nota', 'creada',
            'revisada_por', 'cambiar_contrasena']
SIN_CONTRASENA = [c for c in COLUMNAS if c not in ('sal', 'hash')]
CAMPOS_EMPRESA = ['nombre', 'cif', 'categoria', 'pais', 'ciudad', 'direccion', 'telefono', 'web']


# ----------------------------------------------------------------------------- contraseñas y códigos
def cifrar(contrasena, sal=None):
    """(sal, hash) en hexadecimal. Nunca se guarda la contraseña."""
    sal = sal or secrets.token_hex(16)
    return sal, hashlib.pbkdf2_hmac('sha256', contrasena.encode(), bytes.fromhex(sal), ITERACIONES).hex()


def comprobar(contrasena, sal, hash_guardado):
    return hmac.compare_digest(cifrar(contrasena, sal)[1], hash_guardado)


def contrasena_valida(contrasena):
    """Al menos 8 caracteres y al menos un símbolo (algo que no sea letra ni número: ! . - _ @ ...)."""
    if len(contrasena) < MINIMO_CONTRASENA or contrasena.isalnum():
        raise HTTPException(422, f'La contraseña tiene que tener al menos {MINIMO_CONTRASENA} caracteres y algún '
                                 'símbolo (por ejemplo ! . - _ @)')
    return contrasena


def contrasena_provisional():
    """Fácil de dictar (sin letras que se confundan, como 0/O o 1/l) y con un símbolo, como las demás."""
    letras = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    texto = ''.join(secrets.choice(letras) for _ in range(9))
    return f'{texto[:5]}-{texto[5:]}'


def huella_codigo(email, codigo):
    return hashlib.sha256(f'{email}:{codigo}'.encode()).hexdigest()


def es_invitado(usuario):
    return usuario['email'].endswith(DOMINIO_INVITADO)


def ve_todo(usuario):
    return usuario['rol'] in DE_LA_ENTIDAD


def _con_activo(fila):
    if fila is not None:
        fila['activo'] = int(fila['estado'] == 'activa')
    return fila


# ----------------------------------------------------------------------------- almacén
class Almacen:
    """Cuentas, sesiones, códigos, Mis avisos y empresas: la base de datos de la aplicación (web/bd_cuentas.py,
    PostgreSQL con CUENTAS_URL o SQLite en local). bd: una BaseCuentas (por defecto, la de CUENTAS_URL)."""

    def __init__(self, bd=None):
        self.bd = bd or BaseCuentas()
        self._cache = {}

    def preparar(self):
        self.bd.preparar()

    # ------------------------------------------------------------------ cuentas
    def ficha(self, email):
        """La cuenta con ese correo en cualquier estado (con la contraseña cifrada), o None."""
        return _con_activo(self.bd.fila(f'SELECT {", ".join(COLUMNAS)} FROM usuarios WHERE email = ?',
                                        [email.strip().lower()]))

    def usuario(self, email):
        """La cuenta si puede entrar (activa), o None."""
        u = self.ficha(email)
        return u if u and u['activo'] else None

    def cuentas(self):
        """Todas las cuentas, sin contraseñas: primero las de la entidad y luego las de comercios."""
        return [_con_activo(f) for f in self.bd.filas(
            f"SELECT {', '.join(SIN_CONTRASENA)} FROM usuarios "
            "ORDER BY rol = 'comercio', estado != 'activa', creada DESC")]

    def _escribir(self, fila):
        self.bd.guardar('usuarios', {c: fila[c] for c in COLUMNAS}, 'email')
        self._cache.clear()                       # un cambio se nota al momento en este proceso

    def guardar_usuario(self, email, nombre, rol, comercio_id, contrasena, activo=True, provisional=False,
                        cifrada=None, **extra):
        """Crea la cuenta o la reescribe con una contraseña nueva. provisional: hay que cambiarla al entrar.
        cifrada: (sal, hash) en vez de la contraseña (el registro la cifra antes de confirmar el correo).
        extra: telefono, nota o revisada_por."""
        if rol not in ROLES:
            raise ValueError(f'rol desconocido: {rol} ({", ".join(ROLES)})')
        if rol == 'comercio' and not comercio_id:
            raise ValueError('un usuario de comercio necesita su comercio_id')
        antes = self.ficha(email) or {'telefono': '', 'nota': '', 'revisada_por': '', 'creada': ahora()}
        sal, hash_ = cifrada or cifrar(contrasena)
        self._escribir({**antes, **extra, 'email': email.strip().lower(), 'nombre': nombre, 'rol': rol,
                        'comercio_id': int(comercio_id or 0) if rol == 'comercio' else 0, 'sal': sal, 'hash': hash_,
                        'estado': 'activa' if activo else 'desactivada', 'cambiar_contrasena': int(provisional)})

    def cambiar_estado(self, email, estado, revisada_por=''):
        """Desactivar o reactivar una cuenta sin tocar su contraseña. None si no existe."""
        if estado not in ESTADOS:
            raise ValueError(f'estado desconocido: {estado}')
        u = self.ficha(email)
        if u is None:
            return None
        self._escribir({**u, 'estado': estado, 'revisada_por': revisada_por or u['revisada_por']})
        return _con_activo({**u, 'estado': estado})

    def cambiar_datos(self, email, **datos):
        """Nombre o teléfono (desde Mi perfil), sin tocar la contraseña ni el estado."""
        u = self.ficha(email)
        self._escribir({**u, **{k: v for k, v in datos.items() if k in ('nombre', 'telefono')}})

    def cambiar_contrasena(self, email, contrasena, provisional=False):
        u = self.ficha(email)
        sal, hash_ = cifrar(contrasena)
        self._escribir({**u, 'sal': sal, 'hash': hash_, 'cambiar_contrasena': int(provisional)})

    def borrar_usuarios(self, terminados_en):
        """Para los tests: borra las cuentas (y lo suyo) de un dominio de prueba."""
        patron = f'%{terminados_en}'
        for tabla in ('usuarios', 'sesiones', 'codigos', 'ajustes_usuario'):
            self.bd.ejecutar(f'DELETE FROM {tabla} WHERE email LIKE ?', [patron])
        self._cache.clear()

    # ------------------------------------------------------------------ registro de actividad
    def registrar(self, email, accion, detalle=''):
        """Apunta quién hizo qué. Nunca hace fallar lo que se estaba haciendo: si no se puede, se avisa y sigue."""
        try:
            self.bd.ejecutar('INSERT INTO registro (ts, email, accion, detalle) VALUES (?, ?, ?, ?)',
                             [ahora(), (email or '').strip().lower(), accion, str(detalle)[:500]])
        except Exception as e:
            print(f'Registro de actividad: no se ha podido apuntar {accion} de {email} ({str(e)[:120]})')

    def actividad(self, limite=200, email=None):
        """Lo último que ha pasado (lo más reciente primero), de todos o de una persona."""
        if email:
            return self.bd.filas('SELECT ts, email, accion, detalle FROM registro WHERE email = ? '
                                 'ORDER BY ts DESC LIMIT ?', [email.strip().lower(), int(limite)])
        return self.bd.filas('SELECT ts, email, accion, detalle FROM registro ORDER BY ts DESC LIMIT ?', [int(limite)])

    # ------------------------------------------------------------------ avisos leídos (de cada persona)
    def avisos_leidos(self, email):
        return {f['aviso_id'] for f in self.bd.filas('SELECT aviso_id FROM avisos_leidos WHERE email = ?', [email])}

    def marcar_leidos(self, email, aviso_ids):
        for aviso_id in aviso_ids:
            self.bd.guardar('avisos_leidos', {'email': email, 'aviso_id': str(aviso_id), 'leido': ahora()},
                            ['email', 'aviso_id'])

    # ------------------------------------------------------------------ revisiones de la web (web/revisiones.py)
    def guardar_revision(self, fila):
        self.bd.guardar('revisiones', fila, ['transaccion_id', 'fecha_real'])

    def revisiones(self):
        return self.bd.filas('SELECT transaccion_id, veredicto, comentario, usuario, email, comercio, fecha_revision, '
                             'fecha_real, datos FROM revisiones ORDER BY fecha_real')

    # ------------------------------------------------------------------ Mis avisos (web/ajustes.py)
    def ajustes(self, email):
        return {f['clave']: f['valor'] for f in self.bd.filas(
            'SELECT clave, valor FROM ajustes_usuario WHERE email = ?', [email])}

    def guardar_ajustes(self, email, cambios):
        for clave, valor in cambios.items():
            self.bd.guardar('ajustes_usuario', {'email': email, 'clave': clave, 'valor': valor}, ['email', 'clave'])

    # ------------------------------------------------------------------ empresas (web/cuentas.py)
    def empresa(self, comercio_id):
        return self.bd.fila(f'SELECT {", ".join(CAMPOS_EMPRESA)}, registrada, configurada, alta, tamano FROM empresa '
                            'WHERE comercio_id = ?', [comercio_id])

    def guardar_empresa(self, comercio_id, datos, registrada, configurada, alta=None, tamano=None):
        """alta (AAAA-MM-DD) y tamano solo se dan al registrarse: si no se pasan, se dejan como estaban."""
        fila = {'comercio_id': comercio_id, **{k: datos.get(k, '') for k in CAMPOS_EMPRESA},
                'registrada': int(registrada), 'configurada': int(configurada)}
        fila.update({k: v for k, v in (('alta', alta), ('tamano', tamano)) if v is not None})
        self.bd.guardar('empresa', fila, 'comercio_id')

    def empresas_registradas(self):
        return self.bd.filas(f'SELECT comercio_id, {", ".join(CAMPOS_EMPRESA)}, alta, tamano FROM empresa '
                             'WHERE registrada = 1 ORDER BY comercio_id')

    def ultimo_comercio_registrado(self):
        f = self.bd.fila('SELECT max(comercio_id) AS m FROM empresa')
        return (f or {}).get('m') or 0

    def borrar_empresas(self, comercio_ids):
        for c in comercio_ids:
            self.bd.ejecutar('DELETE FROM empresa WHERE comercio_id = ?', [c])

    # ------------------------------------------------------------------ códigos de 6 cifras
    def nuevo_codigo(self, email, proposito, datos=''):
        """Un código para ese correo (el anterior del mismo propósito deja de valer). Devuelve el código."""
        email = email.strip().lower()
        codigo = f'{secrets.randbelow(10 ** 6):06d}'
        self.bd.guardar('codigos', {'email': email, 'proposito': proposito, 'codigo_hash': huella_codigo(email, codigo),
                                    'datos': datos, 'caduca': ahora() + int(DURACION_CODIGO.total_seconds()),
                                    'intentos': 0, 'usado': 0}, ['email', 'proposito'])
        return codigo

    def usar_codigo(self, email, proposito, codigo):
        """Comprueba el código y lo gasta. Devuelve sus datos, o lanza un 422 que dice por qué no vale."""
        email = email.strip().lower()
        c = self.bd.fila('SELECT codigo_hash, datos, caduca, intentos, usado FROM codigos '
                         'WHERE email = ? AND proposito = ?', [email, proposito])
        if c is None or c['usado']:
            raise HTTPException(422, 'Pide un código nuevo')
        if c['caduca'] <= ahora():
            raise HTTPException(422, 'El código ha caducado (dura 15 minutos): pide uno nuevo')
        if c['intentos'] >= INTENTOS_CODIGO:
            raise HTTPException(422, 'Demasiados intentos: pide un código nuevo')
        if not hmac.compare_digest(huella_codigo(email, codigo.strip()), c['codigo_hash']):
            self.bd.ejecutar('UPDATE codigos SET intentos = intentos + 1 WHERE email = ? AND proposito = ?',
                             [email, proposito])
            quedan = INTENTOS_CODIGO - c['intentos'] - 1
            raise HTTPException(422, f'El código no es correcto (te quedan {quedan} intento{"s" if quedan != 1 else ""})')
        self.bd.ejecutar('UPDATE codigos SET usado = 1 WHERE email = ? AND proposito = ?', [email, proposito])
        return c['datos']

    # ------------------------------------------------------------------ sesiones
    def abrir_sesion(self, email, duracion):
        token = secrets.token_urlsafe(32)
        self.bd.guardar('sesiones', {'token': token, 'email': email,
                                     'caduca': ahora() + int(duracion.total_seconds()), 'cerrada': 0}, 'token')
        return token

    def cerrar_sesion(self, token):
        self._cache.pop(token, None)
        self.bd.ejecutar('UPDATE sesiones SET cerrada = 1 WHERE token = ?', [token])

    def sesiones_abiertas(self, email):
        """Los tokens de las sesiones de esa cuenta que siguen valiendo (en cualquier equipo)."""
        return [f['token'] for f in self.bd.filas(
            'SELECT token FROM sesiones WHERE email = ? AND cerrada = 0 AND caduca > ?', [email, ahora()])]

    def cerrar_otras(self, email, menos=None):
        """Cierra todas las sesiones de esa cuenta menos la de este equipo. Devuelve cuántas ha cerrado."""
        otras = [t for t in self.sesiones_abiertas(email) if t != menos]
        for t in otras:
            self.bd.ejecutar('UPDATE sesiones SET cerrada = 1 WHERE token = ?', [t])
            self._cache.pop(t, None)
        return len(otras)

    def de_token(self, token):
        """El usuario de esa sesión (dict sin la contraseña) o None si no existe, caducó o se cerró."""
        if not token:
            return None
        guardado = self._cache.get(token)
        if guardado and time.monotonic() - guardado[1] < CACHE_SEGUNDOS:
            return guardado[0]
        try:
            usuario = self.bd.fila('''
                SELECT u.email AS email, u.nombre AS nombre, u.rol AS rol, u.comercio_id AS comercio_id,
                       u.cambiar_contrasena AS cambiar_contrasena
                FROM sesiones AS s INNER JOIN usuarios AS u ON u.email = s.email
                WHERE s.token = ? AND s.cerrada = 0 AND s.caduca > ? AND u.estado = 'activa\'''', [token, ahora()])
        except Exception:
            return None
        self._cache[token] = (usuario, time.monotonic())
        return usuario


# ----------------------------------------------------------------------------- middleware
def _json(status, detalle, **extra):
    return JSONResponse({'detail': detalle, **extra}, status_code=status)


class ProtegerWeb:
    """Middleware ASGI. publicas: rutas que no piden sesión (además de /static/). solo_roles: si se da, toda
    la web es solo para esos roles (la de modelos, para el administrador). rutas_admin: prefijos que solo
    puede usar un administrador (la gestión de cuentas). login: función (request) -> dirección del login."""

    def __init__(self, app, almacen, publicas=(), solo_roles=(), rutas_admin=(), login=None):
        self.app = app
        self.almacen = almacen
        self.publicas = set(publicas)
        self.solo_roles = tuple(solo_roles)
        self.rutas_admin = tuple(rutas_admin)
        self.login = login or (lambda request: '/login')

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        # La ruta dentro de esta web: publicada, la de modelos cuelga de /modelos/ (despliegue/contenedor/unida.py)
        ruta = scope['path']
        raiz = scope.get('root_path', '')
        if raiz and ruta.startswith(raiz):
            ruta = ruta[len(raiz):] or '/'
        if ruta.startswith('/static/') or ruta in self.publicas:
            return await self.app(scope, receive, send)

        request = Request(scope)
        usuario = await run_in_threadpool(self.almacen().de_token, request.cookies.get(COOKIE))
        es_api = ruta.startswith('/api/') or '/api/' in scope['path']   # también /modelos/api/... vista desde fuera
        if usuario is None:
            if es_api:
                return await _json(401, 'Inicia sesión para ver estos datos')(scope, receive, send)
            destino = f'{self.login(request)}?volver={quote(str(request.url), safe="")}'
            return await RedirectResponse(destino, status_code=303)(scope, receive, send)
        if es_invitado(usuario) and scope['method'] not in ('GET', 'HEAD') and ruta != '/api/logout':
            return await _json(403, 'Estás como invitado: puedes mirarlo todo, pero no cambiar nada')(scope, receive, send)
        if usuario['cambiar_contrasena'] and ruta not in SIN_CAMBIAR_CONTRASENA:
            if es_api:
                return await _json(403, 'Antes tienes que cambiar tu contraseña provisional',
                                   cambiar_contrasena=True)(scope, receive, send)
            return await RedirectResponse(f'{self.login(request)}?cambiar=1', status_code=303)(scope, receive, send)
        if usuario['rol'] != 'administrador' and self.rutas_admin and ruta.startswith(self.rutas_admin):
            return await _json(403, 'Solo un administrador de la entidad puede hacer esto')(scope, receive, send)
        if self.solo_roles and usuario['rol'] not in self.solo_roles:
            if es_api:
                return await _json(403, 'Tu cuenta no tiene acceso a esta página')(scope, receive, send)
            return await RedirectResponse(self.login(request).rsplit('/login', 1)[0] + '/', status_code=303)(scope, receive, send)

        scope['usuario'] = usuario
        if usuario['rol'] == 'comercio':
            # Un comercio solo ve lo suyo y nunca la verdad del simulador
            parametros = [(k, v) for k, v in parse_qsl(scope.get('query_string', b'').decode(), keep_blank_values=True)
                          if k not in ('comercio', 'demo')]
            scope['query_string'] = urlencode(parametros + [('comercio', str(usuario['comercio_id']))]).encode()
        if scope['method'] == 'POST' and es_api:
            receive = await self._cuerpo_firmado(receive, usuario)
        return await self.app(scope, receive, send)

    @staticmethod
    async def _cuerpo_firmado(receive, usuario):
        """En los JSON que se envían, quién es y de qué comercio lo pone la sesión, no la página."""
        partes, mas = [], True
        while mas:
            mensaje = await receive()
            partes.append(mensaje.get('body', b''))
            mas = mensaje.get('more_body', False)
        cuerpo = b''.join(partes)
        try:
            datos = json.loads(cuerpo) if cuerpo else None
        except ValueError:
            datos = None
        if isinstance(datos, dict):
            if 'usuario' in datos:
                datos['usuario'] = usuario['nombre'][:80]
            if 'comercio' in datos or usuario['rol'] == 'comercio':
                datos['comercio'] = usuario['comercio_id'] if usuario['rol'] == 'comercio' else datos.get('comercio', 0)
            cuerpo = json.dumps(datos).encode()
        enviado = False

        async def nuevo_receive():
            nonlocal enviado
            if not enviado:
                enviado = True
                return {'type': 'http.request', 'body': cuerpo, 'more_body': False}
            return await receive()
        return nuevo_receive


def usuario_de(request):
    """El usuario de la petición (lo deja el middleware)."""
    return request.scope.get('usuario')


# ----------------------------------------------------------------------------- rutas
class Perfil(BaseModel):
    nombre: Optional[str] = Field(None, min_length=2, max_length=80)
    telefono: Optional[str] = Field(None, max_length=30)


class CambioContrasena(BaseModel):
    actual: str = Field(max_length=200)
    nueva: str = Field(max_length=200)


class Acceso(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    contrasena: str = Field(min_length=1, max_length=200)
    recordar: bool = False


def con_sesion(respuesta, request, almacen, email, recordar):
    """Abre una sesión para esa cuenta y la guarda en la cookie de la respuesta."""
    duracion = DURACION_RECORDAR if recordar else DURACION_NORMAL
    token = almacen.abrir_sesion(email, duracion)
    segura = request.headers.get('x-forwarded-proto', request.url.scheme) == 'https'
    respuesta.set_cookie(COOKIE, token, httponly=True, samesite='lax', secure=segura, path='/',
                         max_age=int(duracion.total_seconds()) if recordar else None)
    return respuesta


def crear_router(almacen, nombre_comercio=lambda comercio_id: None):
    """almacen: función que devuelve el Almacen. nombre_comercio: para enseñar el nombre en la cabecera."""
    router = APIRouter()

    @router.get('/login', include_in_schema=False)
    def pagina_login():
        return FileResponse(LOGIN_HTML, headers={'Cache-Control': 'no-cache'})

    @router.post('/api/login')
    def entrar(a: Acceso, request: Request):
        try:
            u = almacen().ficha(a.email)
        except Exception as e:
            raise HTTPException(503, f'No se puede comprobar la cuenta: ¿está ClickHouse levantado? ({str(e)[:120]})')
        if u is None or not comprobar(a.contrasena, u['sal'], u['hash']):
            almacen().registrar(a.email, 'entrar_fallido', 'correo o contraseña incorrectos')
            time.sleep(0.5)                          # frena a quien prueba contraseñas
            raise HTTPException(401, 'Correo o contraseña incorrectos')
        if not u['activo']:                      # solo se dice si la contraseña es la buena
            almacen().registrar(u['email'], 'entrar_fallido', 'cuenta desactivada')
            raise HTTPException(403, 'Esta cuenta está desactivada. Contacta con el administrador de tu entidad.')
        almacen().registrar(u['email'], 'entrar', 'mantener la sesión' if a.recordar else '')
        return con_sesion(JSONResponse({'nombre': u['nombre'], 'rol': u['rol'],
                                        'cambiar_contrasena': bool(u['cambiar_contrasena'])}),
                          request, almacen(), u['email'], a.recordar)

    @router.post('/api/logout')
    def salir(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            u = almacen().de_token(token)
            almacen().cerrar_sesion(token)
            if u:
                almacen().registrar(u['email'], 'salir')
        r = JSONResponse({'ok': True})
        r.delete_cookie(COOKIE, path='/')
        return r

    @router.get('/api/sesion')
    def sesion(request: Request):
        u = usuario_de(request)
        return {**u, 'cambiar_contrasena': bool(u['cambiar_contrasena']), 'invitado': es_invitado(u),
                'comercio_nombre': nombre_comercio(u['comercio_id']) if u['rol'] == 'comercio' else None}

    # ------------------------------------------------------------------ Mi perfil
    @router.get('/api/perfil')
    def perfil(request: Request):
        f = almacen().ficha(usuario_de(request)['email'])
        return {'email': f['email'], 'nombre': f['nombre'], 'telefono': f['telefono'], 'rol': f['rol'],
                'comercio': nombre_comercio(f['comercio_id']) if f['rol'] == 'comercio' else None,
                'comercio_id': f['comercio_id'], 'creada': f['creada'],
                'otras_sesiones': len(almacen().sesiones_abiertas(f['email'])) - 1}

    @router.post('/api/perfil')
    def guardar_perfil(p: Perfil, request: Request):
        cambios = {k: v.strip() for k, v in p.model_dump().items() if v is not None}
        if cambios:
            almacen().cambiar_datos(usuario_de(request)['email'], **cambios)
            almacen().registrar(usuario_de(request)['email'], 'cambiar_perfil', ', '.join(cambios))
        return perfil(request)

    @router.post('/api/perfil/contrasena')
    def cambiar_contrasena(c: CambioContrasena, request: Request):
        """Pide la actual (por si alguien usa un equipo con la sesión abierta) y cierra las demás sesiones:
        si alguien conocía la antigua, deja de tener acceso. También sirve para cambiar la provisional."""
        email = usuario_de(request)['email']
        f = almacen().ficha(email)
        if not comprobar(c.actual, f['sal'], f['hash']):
            time.sleep(0.5)
            raise HTTPException(422, 'La contraseña actual no es correcta')
        contrasena_valida(c.nueva)
        if c.nueva == c.actual:
            raise HTTPException(422, 'La contraseña nueva tiene que ser distinta de la actual')
        provisional = bool(f['cambiar_contrasena'])
        almacen().cambiar_contrasena(email, c.nueva)
        almacen().registrar(email, 'cambiar_contrasena', 'la provisional' if provisional else '')
        return {'cerradas': almacen().cerrar_otras(email, request.cookies.get(COOKIE))}

    @router.post('/api/perfil/cerrar-otras')
    def cerrar_otras(request: Request):
        """Cerrar la sesión en todos los demás equipos (por ejemplo, si se dejó abierta en otro ordenador)."""
        email = usuario_de(request)['email']
        cerradas = almacen().cerrar_otras(email, request.cookies.get(COOKIE))
        almacen().registrar(email, 'cerrar_otras_sesiones', f'{cerradas} sesiones')
        return {'cerradas': cerradas}

    return router
