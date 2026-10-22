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

Contraseñas con PBKDF2-SHA256 (biblioteca estándar) y sesiones en ClickHouse (tabla sesiones): cerrar
sesión las invalida de verdad y la web de modelos, en otro puerto, puede comprobarlas (el navegador
manda la misma cookie a los dos puertos de la misma máquina).
"""
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

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

TABLAS = ['''
    CREATE TABLE IF NOT EXISTS usuarios (
        email       String,
        nombre      String,
        rol         LowCardinality(String),       -- administrador, analista o comercio
        comercio_id UInt32,                       -- 0 para los de la entidad
        sal         String,
        hash        String,                       -- PBKDF2-SHA256 de la contraseña
        activo      UInt8,                        -- puede entrar (estado = activa)
        actualizado DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(actualizado) ORDER BY email''', '''
    CREATE TABLE IF NOT EXISTS sesiones (
        token       String,
        email       String,
        caduca      DateTime,
        cerrada     UInt8,
        ts          DateTime64(3) DEFAULT now64(3)
    ) ENGINE = ReplacingMergeTree(ts) ORDER BY token''', '''
    CREATE TABLE IF NOT EXISTS codigos (
        email       String,
        proposito   LowCardinality(String),       -- recuperar (contraseña olvidada) o registro (comercio nuevo)
        codigo_hash String,                       -- el código solo lo tiene quien recibe el correo
        datos       String,                       -- registro: lo que se ha rellenado, hasta confirmar el correo
        caduca      DateTime,
        intentos    UInt8,
        usado       UInt8,
        ts          DateTime64(3) DEFAULT now64(3)
    ) ENGINE = ReplacingMergeTree(ts) ORDER BY (email, proposito)''',
    # Columnas que se añadieron después (las tablas de antes las ganan al arrancar)
    '''ALTER TABLE usuarios
        ADD COLUMN IF NOT EXISTS estado LowCardinality(String) DEFAULT if(activo, 'activa', 'desactivada'),
        ADD COLUMN IF NOT EXISTS telefono String DEFAULT '',
        ADD COLUMN IF NOT EXISTS nota String DEFAULT '',
        ADD COLUMN IF NOT EXISTS creada DateTime DEFAULT now(),
        ADD COLUMN IF NOT EXISTS revisada_por String DEFAULT '',
        ADD COLUMN IF NOT EXISTS cambiar_contrasena UInt8 DEFAULT 0''']
COLUMNAS = ['email', 'nombre', 'rol', 'comercio_id', 'sal', 'hash', 'activo', 'estado', 'telefono', 'nota', 'creada',
            'revisada_por', 'cambiar_contrasena']
COLUMNAS_CODIGO = ['email', 'proposito', 'codigo_hash', 'datos', 'caduca', 'intentos', 'usado']


# ----------------------------------------------------------------------------- contraseñas y códigos
def cifrar(contrasena, sal=None):
    """(sal, hash) en hexadecimal. Nunca se guarda la contraseña."""
    sal = sal or secrets.token_hex(16)
    return sal, hashlib.pbkdf2_hmac('sha256', contrasena.encode(), bytes.fromhex(sal), ITERACIONES).hex()


def comprobar(contrasena, sal, hash_guardado):
    return hmac.compare_digest(cifrar(contrasena, sal)[1], hash_guardado)


def contrasena_valida(contrasena):
    if len(contrasena) < MINIMO_CONTRASENA:
        raise HTTPException(422, f'La contraseña tiene que tener al menos {MINIMO_CONTRASENA} caracteres')
    return contrasena


def contrasena_provisional():
    """Fácil de dictar: sin letras que se confundan (0/O, 1/l)."""
    letras = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(letras) for _ in range(10))


def huella_codigo(email, codigo):
    return hashlib.sha256(f'{email}:{codigo}'.encode()).hexdigest()


def en_utc(fila, *columnas):
    """ClickHouse devuelve las fechas en UTC sin decirlo: se marcan, para que al volver a guardarlas no se
    tomen por la hora local (y se atrasen o caduquen antes de tiempo)."""
    for c in columnas:
        if isinstance(fila.get(c), datetime) and fila[c].tzinfo is None:
            fila[c] = fila[c].replace(tzinfo=timezone.utc)
    return fila


def ve_todo(usuario):
    return usuario['rol'] in DE_LA_ENTIDAD


# ----------------------------------------------------------------------------- almacén
class Almacen:
    """Usuarios, sesiones y códigos en ClickHouse. cliente: función que devuelve la conexión."""

    def __init__(self, cliente):
        self.cliente = cliente
        self._cache = {}
        self._tablas = False

    def preparar(self):
        if not self._tablas:
            for sql in TABLAS:
                self.cliente().command(sql)
            self._tablas = True

    # ------------------------------------------------------------------ cuentas
    def ficha(self, email):
        """La cuenta con ese correo en cualquier estado (con la contraseña cifrada), o None."""
        self.preparar()
        filas = self.cliente().query(f'SELECT {", ".join(COLUMNAS)} FROM usuarios FINAL WHERE email = {{e:String}}',
                                     parameters={'e': email.strip().lower()}).named_results()
        fila = next(iter(filas), None)
        return en_utc(fila, 'creada') if fila else None

    def usuario(self, email):
        """La cuenta si puede entrar (activa), o None."""
        u = self.ficha(email)
        return u if u and u['activo'] else None

    def cuentas(self):
        """Todas las cuentas, sin contraseñas: primero las de la entidad y luego las de comercios."""
        self.preparar()
        return list(self.cliente().query(f'''
            SELECT {", ".join(c for c in COLUMNAS if c not in ('sal', 'hash'))} FROM usuarios FINAL
            ORDER BY rol = 'comercio', estado != 'activa', creada DESC''').named_results())

    def _escribir(self, fila):
        fila['activo'] = int(fila['estado'] == 'activa')
        self.cliente().insert('usuarios', [[fila[c] for c in COLUMNAS]], column_names=COLUMNAS)
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
        antes = self.ficha(email) or {'telefono': '', 'nota': '', 'revisada_por': '', 'creada': datetime.now()}
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
        return {**u, 'estado': estado}

    def cambiar_datos(self, email, **datos):
        """Nombre o teléfono (desde Mi perfil), sin tocar la contraseña ni el estado."""
        u = self.ficha(email)
        self._escribir({**u, **{k: v for k, v in datos.items() if k in ('nombre', 'telefono')}})

    def cambiar_contrasena(self, email, contrasena, provisional=False):
        u = self.ficha(email)
        sal, hash_ = cifrar(contrasena)
        self._escribir({**u, 'sal': sal, 'hash': hash_, 'cambiar_contrasena': int(provisional)})

    # ------------------------------------------------------------------ códigos de 6 cifras
    def nuevo_codigo(self, email, proposito, datos=''):
        """Un código para ese correo (el anterior del mismo propósito deja de valer). Devuelve el código."""
        self.preparar()
        email = email.strip().lower()
        codigo = f'{secrets.randbelow(10 ** 6):06d}'
        self._codigo({'email': email, 'proposito': proposito, 'codigo_hash': huella_codigo(email, codigo),
                      'datos': datos, 'caduca': datetime.now() + DURACION_CODIGO, 'intentos': 0, 'usado': 0})
        return codigo

    def _codigo(self, fila):
        self.cliente().insert('codigos', [[fila[c] for c in COLUMNAS_CODIGO]], column_names=COLUMNAS_CODIGO)

    def usar_codigo(self, email, proposito, codigo):
        """Comprueba el código y lo gasta. Devuelve sus datos, o lanza un 422 que dice por qué no vale."""
        self.preparar()
        email = email.strip().lower()
        filas = list(self.cliente().query(f'''
            SELECT {", ".join(COLUMNAS_CODIGO)}, caduca > now() AS vigente FROM codigos FINAL
            WHERE email = {{e:String}} AND proposito = {{p:String}}''',
            parameters={'e': email, 'p': proposito}).named_results())
        c = en_utc(filas[0], 'caduca') if filas else None
        if c is None or c['usado']:
            raise HTTPException(422, 'Pide un código nuevo')
        if not c.pop('vigente'):
            raise HTTPException(422, 'El código ha caducado (dura 15 minutos): pide uno nuevo')
        if c['intentos'] >= INTENTOS_CODIGO:
            raise HTTPException(422, 'Demasiados intentos: pide un código nuevo')
        if not hmac.compare_digest(huella_codigo(email, codigo.strip()), c['codigo_hash']):
            self._codigo({**c, 'intentos': c['intentos'] + 1})
            quedan = INTENTOS_CODIGO - c['intentos'] - 1
            raise HTTPException(422, f'El código no es correcto (te quedan {quedan} intento{"s" if quedan != 1 else ""})')
        self._codigo({**c, 'usado': 1})
        return c['datos']

    # ------------------------------------------------------------------ sesiones
    def abrir_sesion(self, email, duracion):
        self.preparar()
        token = secrets.token_urlsafe(32)
        self.cliente().insert('sesiones', [[token, email, datetime.now() + duracion, 0]],
                              column_names=['token', 'email', 'caduca', 'cerrada'])
        return token

    def cerrar_sesion(self, token):
        self._cache.pop(token, None)
        self.preparar()
        self.cliente().insert('sesiones', [[token, '', datetime.now(), 1]],
                              column_names=['token', 'email', 'caduca', 'cerrada'])

    def sesiones_abiertas(self, email):
        """Los tokens de las sesiones de esa cuenta que siguen valiendo (en cualquier equipo)."""
        self.preparar()
        return [t for (t,) in self.cliente().query('''
            SELECT token FROM sesiones FINAL WHERE email = {e:String} AND cerrada = 0 AND caduca > now()''',
            parameters={'e': email}).result_rows]

    def cerrar_otras(self, email, menos=None):
        """Cierra todas las sesiones de esa cuenta menos la de este equipo. Devuelve cuántas ha cerrado."""
        otras = [t for t in self.sesiones_abiertas(email) if t != menos]
        if otras:
            self.cliente().insert('sesiones', [[t, '', datetime.now(), 1] for t in otras],
                                  column_names=['token', 'email', 'caduca', 'cerrada'])
            for t in otras:
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
            self.preparar()
            filas = list(self.cliente().query('''
                SELECT u.email AS email, u.nombre AS nombre, u.rol AS rol, u.comercio_id AS comercio_id,
                       u.cambiar_contrasena AS cambiar_contrasena
                FROM (SELECT * FROM sesiones FINAL WHERE token = {t:String}) AS s
                INNER JOIN (SELECT * FROM usuarios FINAL WHERE activo) AS u ON u.email = s.email
                WHERE s.cerrada = 0 AND s.caduca > now()''', parameters={'t': token}).named_results())
        except Exception:
            return None
        usuario = filas[0] if filas else None
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
            time.sleep(0.5)                          # frena a quien prueba contraseñas
            raise HTTPException(401, 'Correo o contraseña incorrectos')
        if not u['activo']:                      # solo se dice si la contraseña es la buena
            raise HTTPException(403, 'Esta cuenta está desactivada. Contacta con el administrador de tu entidad.')
        return con_sesion(JSONResponse({'nombre': u['nombre'], 'rol': u['rol'],
                                        'cambiar_contrasena': bool(u['cambiar_contrasena'])}),
                          request, almacen(), u['email'], a.recordar)

    @router.post('/api/logout')
    def salir(request: Request):
        token = request.cookies.get(COOKIE)
        if token:
            almacen().cerrar_sesion(token)
        r = JSONResponse({'ok': True})
        r.delete_cookie(COOKIE, path='/')
        return r

    @router.get('/api/sesion')
    def sesion(request: Request):
        u = usuario_de(request)
        return {**u, 'cambiar_contrasena': bool(u['cambiar_contrasena']),
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
        almacen().cambiar_contrasena(email, c.nueva)
        return {'cerradas': almacen().cerrar_otras(email, request.cookies.get(COOKIE))}

    @router.post('/api/perfil/cerrar-otras')
    def cerrar_otras(request: Request):
        """Cerrar la sesión en todos los demás equipos (por ejemplo, si se dejó abierta en otro ordenador)."""
        return {'cerradas': almacen().cerrar_otras(usuario_de(request)['email'], request.cookies.get(COOKIE))}

    return router
