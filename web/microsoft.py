"""
Conectar con Microsoft Teams: cada usuario entra con su cuenta de Microsoft (la del trabajo), da permiso a la
aplicación Centinela y elige en qué canal de uno de sus equipos quiere los avisos. Centinela los publica allí
(Microsoft Graph), sin que nadie tenga que copiar direcciones de webhook.

La aplicación se registra una vez en https://entra.microsoft.com (multiinquilino: vale para cualquier organización)
con los permisos delegados offline_access, User.Read, Team.ReadBasic.All, Channel.ReadBasic.All y
ChannelMessage.Send, que no piden al administrador (salvo que la organización no deje dar permisos a sus usuarios).

    MS_CLIENTE_ID   el "Id. de aplicación (cliente)"
    MS_SECRETO      un secreto de cliente de esa aplicación

Lo que se guarda de cada usuario (tabla ajustes_usuario, web/bd_cuentas.py): el permiso para renovar el acceso
(refresh token), cifrado con una clave sacada de MS_SECRETO; el equipo y el canal elegidos. Los mensajes salen a
nombre del usuario (los permisos delegados actúan por él): para que salgan de "Centinela" haría falta un bot de
Teams, que instala el administrador de la organización.

Solo la biblioteca estándar y cryptography: lo usan la web (web/teams.py) y pipeline/notificar.py.
"""
import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

AUTORIDAD = 'https://login.microsoftonline.com/organizations/oauth2/v2.0'
GRAPH = 'https://graph.microsoft.com/v1.0'
PERMISOS = 'offline_access User.Read Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Send'
RUTA_VUELTA = '/api/teams/vuelta'


class ErrorMicrosoft(Exception):
    pass


def configurado():
    return bool(os.environ.get('MS_CLIENTE_ID', '').strip() and os.environ.get('MS_SECRETO', '').strip())


def _cifrador():
    from cryptography.fernet import Fernet
    clave = hashlib.sha256(('centinela-teams:' + os.environ['MS_SECRETO'].strip()).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(clave))


def cifrar(texto):
    return _cifrador().encrypt(texto.encode()).decode()


def descifrar(texto):
    return _cifrador().decrypt(texto.encode()).decode()


def _peticion(url, datos=None, token=None, json_cuerpo=None):
    cabeceras = {'Accept': 'application/json'}
    cuerpo = None
    if datos is not None:
        cuerpo = urllib.parse.urlencode(datos).encode()
        cabeceras['Content-Type'] = 'application/x-www-form-urlencoded'
    if json_cuerpo is not None:
        cuerpo = json.dumps(json_cuerpo).encode()
        cabeceras['Content-Type'] = 'application/json'
    if token:
        cabeceras['Authorization'] = f'Bearer {token}'
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=cuerpo, headers=cabeceras), timeout=20) as r:
            texto = r.read().decode() or '{}'
            return json.loads(texto)
    except urllib.error.HTTPError as e:
        try:
            error = json.loads(e.read().decode())
        except ValueError:
            error = {}
        # Graph: {"error": {"message": ...}}; el inicio de sesión: {"error": "...", "error_description": ...}
        fallo = error.get('error')
        detalle = fallo.get('message') if isinstance(fallo, dict) else error.get('error_description') or fallo
        raise ErrorMicrosoft(f'Microsoft respondió {e.code}: {str(detalle or e.reason)[:300]}')


# ----------------------------------------------------------------------------- conectar (lo usa web/teams.py)
def direccion_login(redirect_uri, estado):
    return f'{AUTORIDAD}/authorize?' + urllib.parse.urlencode({
        'client_id': os.environ['MS_CLIENTE_ID'].strip(), 'response_type': 'code', 'response_mode': 'query',
        'redirect_uri': redirect_uri, 'scope': PERMISOS, 'state': estado, 'prompt': 'select_account'})


def _tokens(datos):
    return _peticion(f'{AUTORIDAD}/token', {'client_id': os.environ['MS_CLIENTE_ID'].strip(),
                                           'client_secret': os.environ['MS_SECRETO'].strip(), 'scope': PERMISOS,
                                           **datos})


def canjear_codigo(codigo, redirect_uri):
    """El código que devuelve Microsoft al volver, por los tokens: {access_token, refresh_token, ...}."""
    return _tokens({'grant_type': 'authorization_code', 'code': codigo, 'redirect_uri': redirect_uri})


def renovar(refresh_token):
    """Un acceso nuevo (dura ~1 hora). Microsoft puede devolver también un refresh token nuevo: hay que guardarlo."""
    return _tokens({'grant_type': 'refresh_token', 'refresh_token': refresh_token})


def yo(token):
    return _peticion(f'{GRAPH}/me?$select=displayName,mail,userPrincipalName', token=token)


def canales(token):
    """[{equipo_id, equipo, canal_id, canal}] de los equipos de los que es miembro."""
    salida = []
    for equipo in _peticion(f'{GRAPH}/me/joinedTeams?$select=id,displayName', token=token).get('value', []):
        for canal in _peticion(f'{GRAPH}/teams/{equipo["id"]}/channels?$select=id,displayName',
                               token=token).get('value', []):
            salida.append({'equipo_id': equipo['id'], 'equipo': equipo['displayName'],
                           'canal_id': canal['id'], 'canal': canal['displayName']})
    return salida


# ----------------------------------------------------------------------------- publicar (lo usa notificar.py)
def _html(texto):
    return (str(texto).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def publicar(token, equipo_id, canal_id, titulo, texto, lineas=()):
    contenido = f'<b>{_html(titulo)}</b><br>{_html(texto)}'
    if lineas:
        contenido += '<ul>' + ''.join(f'<li>{_html(l)}</li>' for l in lineas) + '</ul>'
    return _peticion(f'{GRAPH}/teams/{equipo_id}/channels/{urllib.parse.quote(canal_id)}/messages', token=token,
                     json_cuerpo={'body': {'contentType': 'html', 'content': contenido}})
