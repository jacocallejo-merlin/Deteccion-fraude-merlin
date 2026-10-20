"""
Notificaciones fuera de la web: Microsoft Teams, Slack y correo (SMTP).

Lo usan:
  generar_avisos.py         resumen de los avisos de fraude nuevos de cada ejecución
  main.py y dags/            un paso del pipeline ha fallado
  vigilar_drift.py           los pagos recientes ya no se parecen a los del entrenamiento

Las URL y contraseñas NO van en config.yaml (se subiría a Git): se leen de variables de entorno
(.env en local, el Secret "notificaciones" en Kubernetes). Lo que no está configurado no se envía,
y si un envío falla se avisa por pantalla pero no se para nada: los avisos siguen en la web.

    TEAMS_WEBHOOK_URL       Teams > canal > Flujos de trabajo > "Publicar en un canal cuando se
                            recibe una solicitud de webhook" (también vale un webhook entrante antiguo)
    SLACK_WEBHOOK_URL       Slack > Incoming Webhooks
    SMTP_SERVIDOR, SMTP_PUERTO, SMTP_USUARIO, SMTP_PASSWORD, CORREO_REMITENTE,
    CORREO_DESTINATARIOS    (separados por comas)

Probar la configuración:   python pipeline/notificar.py --prueba
Desde otro programa:       python pipeline/notificar.py --tipo fallos_pipeline --nivel error --titulo ... --texto ...

No importa config.py (que carga numpy y el resto del pipeline): solo la biblioteca estándar y PyYAML,
para que el aviso de un fallo funcione aunque lo que falle sea el propio entorno.
"""
import argparse
import json
import os
import smtplib
import sys
import urllib.request
from email.message import EmailMessage
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv
    load_dotenv(RAIZ / '.env')                    # en local; en Docker/Kubernetes ya vienen en el entorno
except ImportError:
    pass

with open(RAIZ / 'config.yaml', encoding='utf-8') as f:
    _cfg = yaml.safe_load(f).get('notificaciones') or {}

# Color de la tarjeta de Teams y etiqueta del asunto según el nivel
COLOR_TEAMS = {'critico': 'Attention', 'error': 'Attention', 'alto': 'Warning', 'aviso': 'Warning',
               'info': 'Good'}
NIVELES_AVISO = {'critico': 3, 'alto': 2, 'medio': 1}


def _entorno(nombre, por_defecto=None):
    valor = os.environ.get(nombre, '').strip()
    return valor or por_defecto


def canales():
    """Los canales configurados: {'teams': url, 'slack': url, 'correo': {...}} (solo los que hay)."""
    salida = {}
    if _entorno('TEAMS_WEBHOOK_URL'):
        salida['teams'] = _entorno('TEAMS_WEBHOOK_URL')
    if _entorno('SLACK_WEBHOOK_URL'):
        salida['slack'] = _entorno('SLACK_WEBHOOK_URL')
    correo = _cfg.get('correo') or {}
    servidor = _entorno('SMTP_SERVIDOR', correo.get('servidor'))
    destinatarios = _entorno('CORREO_DESTINATARIOS') or ','.join(correo.get('destinatarios') or [])
    destinatarios = [d.strip() for d in destinatarios.split(',') if d.strip()]
    if servidor and destinatarios:
        usuario = _entorno('SMTP_USUARIO')
        salida['correo'] = {
            'servidor': servidor, 'puerto': int(_entorno('SMTP_PUERTO', correo.get('puerto', 587))),
            'usuario': usuario, 'password': _entorno('SMTP_PASSWORD'),
            'remitente': _entorno('CORREO_REMITENTE', usuario or 'antifraude@localhost'),
            'destinatarios': destinatarios,
        }
    return salida


def _post_json(url, cuerpo):
    peticion = urllib.request.Request(url, data=json.dumps(cuerpo, default=str).encode('utf-8'),
                                      headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(peticion, timeout=10) as r:
        return r.status


def _teams(url, titulo, texto, nivel, lineas):
    # Tarjeta adaptativa: el formato que aceptan los flujos de Teams (y los webhooks entrantes antiguos)
    cuerpo = [{'type': 'TextBlock', 'text': titulo, 'weight': 'Bolder', 'size': 'Medium', 'wrap': True,
               'color': COLOR_TEAMS.get(nivel, 'Default')},
              {'type': 'TextBlock', 'text': texto, 'wrap': True}]
    cuerpo += [{'type': 'TextBlock', 'text': f'- {l}', 'wrap': True, 'spacing': 'Small'} for l in lineas]
    return _post_json(url, {'type': 'message', 'attachments': [{
        'contentType': 'application/vnd.microsoft.card.adaptive', 'contentUrl': None,
        'content': {'$schema': 'http://adaptivecards.io/schemas/adaptive-card.json',
                    'type': 'AdaptiveCard', 'version': '1.4', 'body': cuerpo}}]})


def _slack(url, titulo, texto, nivel, lineas):
    return _post_json(url, {'text': '\n'.join([f'*{titulo}*', texto, *(f'• {l}' for l in lineas)])})


def _correo(c, titulo, texto, nivel, lineas):
    mensaje = EmailMessage()
    mensaje['Subject'] = f'[Antifraude MERLIN] {titulo}'
    mensaje['From'] = c['remitente']
    mensaje['To'] = ', '.join(c['destinatarios'])
    mensaje.set_content('\n'.join([texto, '', *(f'- {l}' for l in lineas)]))
    with smtplib.SMTP(c['servidor'], c['puerto'], timeout=15) as smtp:
        if c['puerto'] != 25:
            smtp.starttls()
        if c['usuario']:
            smtp.login(c['usuario'], c['password'] or '')
        smtp.send_message(mensaje)
    return 'enviado'


ENVIOS = {'teams': _teams, 'slack': _slack, 'correo': _correo}


def notificar(titulo, texto, nivel='info', lineas=(), tipo=None):
    """Envía el mensaje a todos los canales configurados. tipo ('avisos_fraude', 'fallos_pipeline' o
    'drift') permite apagar cada clase de mensaje en config.yaml. Nunca lanza excepciones.
    Devuelve cuántos canales lo han recibido."""
    if tipo and not _cfg.get(tipo, True):
        return 0
    configurados = canales()
    if not configurados:
        print('Notificaciones: no hay ningún canal configurado (Teams, Slack o correo); solo queda en la web y el log')
        return 0
    enviados = 0
    for nombre, destino in configurados.items():
        try:
            resultado = ENVIOS[nombre](destino, titulo, texto, nivel, list(lineas))
            print(f'Notificaciones: enviado a {nombre} ({resultado})')
            enviados += 1
        except Exception as e:
            print(f'Notificaciones: no se ha podido enviar a {nombre} ({str(e).splitlines()[0][:200]})')
    return enviados


def notificar_avisos(avisos):
    """Un solo mensaje con los avisos de fraude nuevos de una ejecución (generar_avisos.py): los
    críticos y más recientes primero, max_lineas en detalle y del resto solo el total. Así la primera
    ejecución, que crea los avisos de todo el histórico, no manda cientos de mensajes."""
    if not avisos:
        return 0
    orden = sorted(avisos, key=lambda a: str(a['dia']), reverse=True)               # los más recientes...
    orden = sorted(orden, key=lambda a: -NIVELES_AVISO.get(a['nivel'], 0))          # ...dentro de cada nivel
    maximo = int(_cfg.get('max_lineas', 10))
    criticos = sum(a['nivel'] == 'critico' for a in avisos)
    lineas = [f'{a["dia"]} [{a["nivel"].upper()}] {a["mensaje"]}' for a in orden[:maximo]]
    if len(avisos) > maximo:
        lineas.append(f'... y {len(avisos) - maximo} más: el detalle está en la web, pestaña Avisos')
    return notificar(f'{len(avisos)} aviso{"s" if len(avisos) != 1 else ""} de fraude nuevo'
                     f'{"s" if len(avisos) != 1 else ""} ({criticos} crítico{"s" if criticos != 1 else ""})',
                     'Casos (misma tarjeta y día) que hay que revisar cuanto antes:',
                     'critico' if criticos else 'alto', lineas, tipo='avisos_fraude')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prueba', action='store_true', help='envía un mensaje de prueba a todos los canales')
    parser.add_argument('--titulo')
    parser.add_argument('--texto', default='')
    parser.add_argument('--nivel', default='info', choices=sorted(COLOR_TEAMS))
    parser.add_argument('--tipo', choices=['avisos_fraude', 'fallos_pipeline', 'drift'])
    args = parser.parse_args()
    if args.prueba:
        print('Canales configurados:', ', '.join(canales()) or 'ninguno')
        enviados = notificar('Prueba de notificaciones', 'Si lees esto, el canal está bien configurado.', 'info',
                             ['Mensaje enviado con python pipeline/notificar.py --prueba'])
        sys.exit(0 if enviados or not canales() else 1)
    if not args.titulo:
        parser.error('falta --titulo (o usa --prueba)')
    notificar(args.titulo, args.texto, args.nivel, tipo=args.tipo)


if __name__ == '__main__':
    main()
