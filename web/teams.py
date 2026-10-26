"""
Mis avisos → Conectar con Microsoft Teams (web/microsoft.py): el botón lleva a iniciar sesión en Microsoft, se da
permiso a Centinela, se vuelve aquí y se elige el canal. Desde entonces pipeline/notificar.py publica en ese canal
los avisos de esa persona. Todo pide sesión en Centinela (web/sesiones.py); el invitado no puede conectar nada.

La dirección de vuelta tiene que ser exactamente una de las registradas en la aplicación de Microsoft:
http://localhost:8000/api/teams/vuelta en local (con "localhost", no 127.0.0.1) y la de la web publicada.
"""
import os
import secrets
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from web import microsoft
from web.sesiones import es_invitado, usuario_de

GALLETA_ESTADO = 'centinela_teams_estado'
CLAVES = ('teams_refresh', 'teams_cuenta', 'teams_equipo_id', 'teams_canal_id', 'teams_equipo', 'teams_canal')


class Canal(BaseModel):
    equipo_id: str = Field(max_length=200)
    canal_id: str = Field(max_length=300)
    equipo: str = Field(max_length=200)
    canal: str = Field(max_length=200)


def vuelta(request):
    """La dirección a la que Microsoft devuelve al usuario: WEB_URL o, si no, la de esta petición."""
    if os.environ.get('WEB_URL'):
        return os.environ['WEB_URL'].rstrip('/') + microsoft.RUTA_VUELTA
    esquema = request.headers.get('x-forwarded-proto', request.url.scheme)
    return f'{esquema}://{request.url.netloc}{microsoft.RUTA_VUELTA}'


def crear_router(almacen):
    """almacen: función que devuelve el Almacen de web/sesiones.py (ahí van los ajustes de cada usuario)."""
    router = APIRouter()

    def quien(request):
        u = usuario_de(request)
        if es_invitado(u):
            raise HTTPException(403, 'Estás como invitado: puedes mirarlo todo, pero no conectar nada')
        if not microsoft.configurado():
            raise HTTPException(503, 'La conexión con Microsoft Teams aún no está configurada en el servidor '
                                     '(MS_CLIENTE_ID y MS_SECRETO)')
        return u

    def acceso(u):
        """Un acceso a Microsoft para ese usuario, renovado con su permiso guardado (y guardando el nuevo)."""
        guardado = almacen().ajustes(u['email']).get('teams_refresh')
        if not guardado:
            raise HTTPException(409, 'Aún no has conectado Microsoft Teams')
        try:
            tokens = microsoft.renovar(microsoft.descifrar(guardado))
        except Exception as e:
            raise HTTPException(409, f'Hay que volver a conectar Microsoft Teams ({str(e)[:150]})')
        if tokens.get('refresh_token'):
            almacen().guardar_ajustes(u['email'], {'teams_refresh': microsoft.cifrar(tokens['refresh_token'])})
        return tokens['access_token']

    @router.get('/api/teams')
    def estado(request: Request):
        a = almacen().ajustes(usuario_de(request)['email'])
        return {'disponible': microsoft.configurado(), 'conectado': bool(a.get('teams_refresh')),
                'cuenta': a.get('teams_cuenta', ''), 'equipo': a.get('teams_equipo', ''),
                'canal': a.get('teams_canal', '') if a.get('teams_canal_id') else ''}

    @router.get('/api/teams/conectar', include_in_schema=False)
    def conectar(request: Request):
        quien(request)
        estado_oauth = secrets.token_urlsafe(24)    # contra que otra web nos cuele su propio inicio de sesión
        r = RedirectResponse(microsoft.direccion_login(vuelta(request), estado_oauth), status_code=303)
        r.set_cookie(GALLETA_ESTADO, estado_oauth, max_age=600, httponly=True, samesite='lax', path='/api/teams',
                     secure=request.headers.get('x-forwarded-proto', request.url.scheme) == 'https')
        return r

    @router.get('/api/teams/vuelta', include_in_schema=False)
    def volver(request: Request, code: str = '', state: str = '', error: str = '', error_description: str = ''):
        u = quien(request)

        def a_la_web(**parametros):
            r = RedirectResponse('/?' + urllib.parse.urlencode(parametros), status_code=303)
            r.delete_cookie(GALLETA_ESTADO, path='/api/teams')
            return r
        if error:
            # p. ej. "consent_required" o "AADSTS65001": la organización no deja dar el permiso sin su administrador
            return a_la_web(teams='error', motivo=(error_description or error)[:300])
        if not state or state != request.cookies.get(GALLETA_ESTADO):
            return a_la_web(teams='error', motivo='La conexión ha caducado o no venía de aquí: vuelve a intentarlo')
        try:
            tokens = microsoft.canjear_codigo(code, vuelta(request))
            perfil = microsoft.yo(tokens['access_token'])
        except Exception as e:
            return a_la_web(teams='error', motivo=str(e)[:300])
        almacen().registrar(u['email'], 'conectar_teams', perfil.get('mail') or perfil.get('userPrincipalName', ''))
        almacen().guardar_ajustes(u['email'], {
            'teams_refresh': microsoft.cifrar(tokens['refresh_token']),
            'teams_cuenta': perfil.get('mail') or perfil.get('userPrincipalName') or perfil.get('displayName', ''),
            'teams_equipo_id': '', 'teams_canal_id': '', 'teams_equipo': '', 'teams_canal': ''})
        return a_la_web(teams='elegir')

    @router.get('/api/teams/canales')
    def lista_canales(request: Request):
        token = acceso(quien(request))
        try:
            return {'canales': microsoft.canales(token)}
        except microsoft.ErrorMicrosoft as e:
            raise HTTPException(502, str(e))

    @router.post('/api/teams/canal')
    def elegir_canal(c: Canal, request: Request):
        u = quien(request)
        token = acceso(u)
        if not any(x['equipo_id'] == c.equipo_id and x['canal_id'] == c.canal_id for x in microsoft.canales(token)):
            raise HTTPException(422, 'Ese canal no es de ninguno de tus equipos')
        almacen().registrar(u['email'], 'canal_teams', f'{c.equipo} > {c.canal}')
        almacen().guardar_ajustes(u['email'], {'teams_equipo_id': c.equipo_id, 'teams_canal_id': c.canal_id,
                                               'teams_equipo': c.equipo, 'teams_canal': c.canal})
        return estado(request)

    @router.post('/api/teams/desconectar')
    def desconectar(request: Request):
        u = usuario_de(request)
        almacen().guardar_ajustes(u['email'], {clave: '' for clave in CLAVES})
        almacen().registrar(u['email'], 'desconectar_teams')
        return JSONResponse({'conectado': False})

    return router
