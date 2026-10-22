"""
Ajustes de avisos desde la web: el cliente pone aquí su canal de Teams (o Slack) y los correos donde
quiere recibir los avisos, sin tocar ficheros del servidor. Se guardan en la tabla ajustes de
ClickHouse y pipeline/notificar.py los lee en cada envío (mandan sobre .env).

Lo que no se configura aquí es del proveedor del servicio: el servidor de correo y su contraseña siguen
en .env / el Secret de Kubernetes. Como el resto de la web, no pide inicio de sesión (ver limitaciones).
"""
import contextlib
import io
import re
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import notificar

CORREO = re.compile(r'^[^@\s,]+@[^@\s,]+\.[^@\s,]+$')
CREAR_TABLA = '''
    CREATE TABLE IF NOT EXISTS ajustes (
        clave       LowCardinality(String),
        valor       String,
        actualizado DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(actualizado)
    ORDER BY clave'''


class Ajustes(BaseModel):
    """Solo se cambia lo que viene (None = se deja como está). Una dirección vacía desactiva el canal."""
    teams_webhook_url: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    correo_destinatarios: Optional[str] = None
    avisos_fraude: Optional[bool] = None
    drift: Optional[bool] = None


def oculta(url):
    """La dirección de un webhook es un secreto: en pantalla solo el final."""
    return f'…{url[-6:]}' if url else ''


def crear_router(cliente):
    """cliente: la función de web/app.py que devuelve la conexión a ClickHouse."""
    router = APIRouter()

    @router.get('/api/ajustes')
    def leer():
        web = notificar.ajustes_web()
        activos = notificar.canales(web)
        correo = activos.get('correo') or {}
        return {
            'teams': {'activo': 'teams' in activos, 'final': oculta(activos.get('teams')),
                      'desde_web': 'teams_webhook_url' in web},
            'slack': {'activo': 'slack' in activos, 'final': oculta(activos.get('slack')),
                      'desde_web': 'slack_webhook_url' in web},
            # Sin servidor de correo (lo pone el proveedor) los correos no se pueden enviar
            'correo_servidor': bool(notificar._entorno('SMTP_SERVIDOR', (notificar._cfg.get('correo') or {}).get('servidor'))),
            'correo_destinatarios': web.get('correo_destinatarios',
                                            ', '.join(correo.get('destinatarios', []))),
            'avisos_fraude': web['avisos_fraude'] == '1' if 'avisos_fraude' in web else notificar._cfg.get('avisos_fraude', True),
            'drift': web['drift'] == '1' if 'drift' in web else notificar._cfg.get('drift', True),
        }

    @router.post('/api/ajustes')
    def guardar(a: Ajustes):
        filas = []
        for clave in ('teams_webhook_url', 'slack_webhook_url'):
            url = getattr(a, clave)
            if url is None:
                continue
            url = url.strip()
            if url and not url.startswith('https://'):
                raise HTTPException(422, 'La dirección del canal tiene que empezar por https://')
            filas.append([clave, url])
        if a.correo_destinatarios is not None:
            correos = [c.strip() for c in a.correo_destinatarios.replace(';', ',').split(',') if c.strip()]
            malos = [c for c in correos if not CORREO.match(c)]
            if malos:
                raise HTTPException(422, f'Correo no válido: {malos[0]}')
            filas.append(['correo_destinatarios', ', '.join(correos)])
        for clave in ('avisos_fraude', 'drift'):
            if getattr(a, clave) is not None:
                filas.append([clave, '1' if getattr(a, clave) else '0'])
        if filas:
            cliente().command(CREAR_TABLA)
            cliente().insert('ajustes', filas, column_names=['clave', 'valor'])
        return leer()

    @router.post('/api/ajustes/prueba')
    def prueba():
        """Manda un mensaje de prueba a los canales activos y devuelve lo que ha pasado con cada uno."""
        salida = io.StringIO()
        with contextlib.redirect_stdout(salida):
            enviados = notificar.notificar('Prueba de avisos de Centinela',
                                           'Si lees esto, este canal recibirá los avisos de fraude.', 'info')
        return {'enviados': enviados, 'detalle': [l for l in salida.getvalue().splitlines() if l.strip()]}

    return router
