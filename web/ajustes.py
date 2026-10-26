"""
Mis avisos: cada usuario de la web elige dónde recibe los avisos de los casos graves, además de en la
web. Por defecto le llegan a su correo (el de su cuenta); si quiere, también a su Teams (su propio chat o
un canal). Se guardan en la base de datos de la aplicación (tabla ajustes_usuario, web/bd_cuentas.py) y
pipeline/notificar.py los lee en cada envío. Un comercio solo recibe los avisos de sus pagos; los del sistema (drift, fallos) son de la entidad.

Lo que no se configura aquí es del proveedor del servicio: el servidor de correo y su contraseña siguen
en .env / el Secret de Kubernetes. Las rutas piden sesión (web/sesiones.py): cada uno ve y cambia lo suyo.
"""
import contextlib
import io
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import notificar
from web.sesiones import usuario_de

class Ajustes(BaseModel):
    """Solo se cambia lo que viene (None = se deja como está). Una dirección de Teams vacía lo desactiva."""
    correo: Optional[bool] = None
    teams_webhook_url: Optional[str] = None
    avisos_fraude: Optional[bool] = None
    sistema: Optional[bool] = None


def oculta(url):
    """La dirección de un webhook es un secreto: en pantalla solo el final."""
    return f'…{url[-6:]}' if url else ''


def crear_router(almacen):
    """almacen: función que devuelve el Almacen de web/sesiones.py (la base de datos de la aplicación)."""
    router = APIRouter()

    def mis_ajustes(u):
        return {**u, **notificar.ajustes_de(u['rol'], almacen().ajustes(u['email']))}

    @router.get('/api/ajustes')
    def leer(request: Request):
        a = mis_ajustes(usuario_de(request))
        return {
            'email': a['email'], 'rol': a['rol'],
            'correo': a['correo'] == '1',
            # Sin servidor de correo (lo pone el proveedor) los correos no se pueden enviar
            'correo_servidor': notificar._correo_config([a['email']]) is not None,
            'teams': {'activo': bool(a['teams_webhook_url']), 'final': oculta(a['teams_webhook_url'])},
            'avisos_fraude': a['avisos_fraude'] == '1',
            'sistema': a['sistema'] == '1' if a['rol'] != 'comercio' else None,
        }

    @router.post('/api/ajustes')
    def guardar(a: Ajustes, request: Request):
        u = usuario_de(request)
        cambios = {}
        if a.teams_webhook_url is not None:
            url = a.teams_webhook_url.strip()
            if url and not url.startswith('https://'):
                raise HTTPException(422, 'La dirección de Teams tiene que empezar por https://')
            cambios['teams_webhook_url'] = url
        for clave in ('correo', 'avisos_fraude') + (('sistema',) if u['rol'] != 'comercio' else ()):
            if getattr(a, clave) is not None:
                cambios[clave] = '1' if getattr(a, clave) else '0'
        almacen().guardar_ajustes(u['email'], cambios)
        if cambios:   # la dirección de Teams es un secreto: solo qué se ha cambiado
            almacen().registrar(u['email'], 'mis_avisos', ', '.join(cambios))
        return leer(request)

    @router.post('/api/ajustes/prueba')
    def prueba(request: Request):
        """Manda un mensaje de prueba a los canales de este usuario y devuelve lo que ha pasado con cada uno."""
        destinos = notificar.canales_de(mis_ajustes(usuario_de(request)))
        if not destinos:
            return {'enviados': 0, 'detalle': ['No tienes ningún canal activo: activa el correo o pon tu Teams.']}
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            enviados = notificar.enviar(destinos, 'Prueba de avisos de Centinela',
                                        'Si lees esto, aquí recibirás los avisos de fraude.', 'info')
        return {'enviados': enviados, 'detalle': [l for l in salida.getvalue().splitlines() if l.strip()]}

    return router
