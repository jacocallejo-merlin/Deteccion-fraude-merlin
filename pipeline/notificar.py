"""
Notificaciones fuera de la web: Microsoft Teams, Slack y correo (SMTP).

Lo usan:
  generar_avisos.py         resumen de los avisos de fraude nuevos de cada ejecución
  main.py y dags/            un paso del pipeline ha fallado
  vigilar_drift.py           los pagos recientes ya no se parecen a los del entrenamiento

A quién:
  - Los canales del servidor (.env en local, el Secret "notificaciones" en Kubernetes), que reciben todo.
    Las URL y contraseñas NO van en config.yaml (se subiría a Git).
  - Cada usuario de la web (base de datos de cuentas, ver web/bd_cuentas.py), según lo que elija en "Mis avisos"
    (tabla ajustes_usuario): por defecto, a su correo; si pone uno, también a su Teams. Un comercio solo
    recibe los avisos de sus pagos; los del sistema (drift, fallos del pipeline) solo los de la entidad (administradores y analistas).
Lo que no está configurado no se envía, y si un envío falla se avisa por pantalla pero no se para nada:
los avisos siguen en la web. El servidor de correo (SMTP_*) es del proveedor y sirve para todos.

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
    _config = yaml.safe_load(f)
_cfg = _config.get('notificaciones') or {}

# Color de la tarjeta de Teams y etiqueta del asunto según el nivel
COLOR_TEAMS = {'critico': 'Attention', 'error': 'Attention', 'alto': 'Warning', 'aviso': 'Warning',
               'info': 'Good'}
NIVELES_AVISO = {'critico': 3, 'alto': 2, 'medio': 1}
# Lo que cada usuario de la web elige en "Mis avisos" (tabla ajustes_usuario) y su valor si no ha tocado
# nada: los avisos le llegan a su correo (el de su cuenta) y, si pone uno, a su Teams. "sistema" (el modelo
# se desvía o falla el pipeline) solo es para la gente de la entidad
AJUSTES_USUARIO = {'correo': '1', 'teams_webhook_url': '', 'avisos_fraude': '1', 'sistema': '1'}


def _entorno(nombre, por_defecto=None):
    valor = os.environ.get(nombre, '').strip()
    return valor or por_defecto


def ajustes_de(rol, guardados):
    """Los ajustes de un usuario: lo que guardó en la web sobre los valores por defecto."""
    ajustes = {**AJUSTES_USUARIO, **{k: v for k, v in guardados.items() if k in AJUSTES_USUARIO}}
    if rol == 'comercio':
        ajustes['sistema'] = '0'
    return ajustes


def usuarios():
    """Los usuarios activos de la web con sus ajustes de avisos: [{email, rol, comercio_id, correo, ...}], de la
    base de datos de cuentas (web/bd_cuentas.py: CUENTAS_URL o SQLite en local). Sin ella o sin usuarios, []:
    nunca impide avisar a los canales del servidor."""
    try:
        sys.path.insert(0, str(RAIZ))
        from web.bd_cuentas import BaseCuentas
        bd = BaseCuentas()
        guardados = {}
        for f in bd.filas('SELECT email, clave, valor FROM ajustes_usuario'):
            guardados.setdefault(f['email'], {})[f['clave']] = f['valor']
        return [{'email': u['email'], 'rol': u['rol'], 'comercio_id': u['comercio_id'],
                 **ajustes_de(u['rol'], guardados.get(u['email'], {}))}
                for u in bd.filas("SELECT email, rol, comercio_id FROM usuarios WHERE estado = 'activa'")
                if not u['email'].endswith('@invitado.centinela')]     # los invitados no reciben avisos
    except Exception:
        return []


def _correo_config(destinatarios):
    """El servidor de correo (del proveedor, en .env) para esos destinatarios; None si no hay servidor."""
    correo = _cfg.get('correo') or {}
    servidor = _entorno('SMTP_SERVIDOR', correo.get('servidor'))
    if not (servidor and destinatarios):
        return None
    usuario = _entorno('SMTP_USUARIO')
    return {'servidor': servidor, 'puerto': int(_entorno('SMTP_PUERTO', correo.get('puerto', 587))),
            'usuario': usuario, 'password': _entorno('SMTP_PASSWORD'),
            'remitente': _entorno('CORREO_REMITENTE', usuario or 'antifraude@localhost'),
            'destinatarios': destinatarios}


def canales():
    """Los canales del servidor (.env / Secret de Kubernetes): {'teams': url, 'slack': url, 'correo': {...}}
    (solo los que hay). Reciben todo; los de cada usuario van aparte (canales_de)."""
    salida = {}
    if _entorno('TEAMS_WEBHOOK_URL'):
        salida['teams'] = _entorno('TEAMS_WEBHOOK_URL')
    if _entorno('SLACK_WEBHOOK_URL'):
        salida['slack'] = _entorno('SLACK_WEBHOOK_URL')
    destinatarios = _entorno('CORREO_DESTINATARIOS') or ','.join((_cfg.get('correo') or {}).get('destinatarios') or [])
    correo = _correo_config([d.strip() for d in destinatarios.split(',') if d.strip()])
    if correo:
        salida['correo'] = correo
    return salida


def canales_de(usuario):
    """Los canales de un usuario de la web: su correo (si lo tiene activado y hay servidor) y su Teams."""
    salida = {}
    if usuario.get('teams_webhook_url'):
        salida['teams'] = usuario['teams_webhook_url']
    correo = _correo_config([usuario['email']]) if usuario.get('correo') == '1' else None
    if correo:
        salida['correo'] = correo
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


def enviar(destinos, titulo, texto, nivel='info', lineas=(), quien=''):
    """Envía a esos canales ({nombre: destino}). Nunca lanza excepciones. Devuelve cuántos lo han recibido."""
    enviados = 0
    for nombre, destino in destinos.items():
        donde = f'{nombre} de {quien}' if quien else nombre
        try:
            resultado = ENVIOS[nombre](destino, titulo, texto, nivel, list(lineas))
            print(f'Notificaciones: enviado a {donde} ({resultado})')
            enviados += 1
        except Exception as e:
            print(f'Notificaciones: no se ha podido enviar a {donde} ({str(e).splitlines()[0][:200]})')
    return enviados


def _sin_canales():
    print('Notificaciones: no hay ningún canal configurado (ni en el servidor ni en los usuarios de la web); '
          'solo queda en la web y el log')


def notificar(titulo, texto, nivel='info', lineas=(), tipo=None):
    """Envía el mensaje a los canales del servidor y, si es del sistema (tipo 'fallos_pipeline' o 'drift'),
    a los administradores y analistas que lo tienen activado en la web. Cada tipo se puede apagar del todo
    en config.yaml. Nunca lanza excepciones. Devuelve cuántos canales lo han recibido."""
    if tipo and not _cfg.get(tipo, True):
        return 0
    destinos = [(canales(), '')]
    if tipo in ('fallos_pipeline', 'drift'):
        destinos += [(canales_de(u), u['email']) for u in usuarios() if u['rol'] != 'comercio' and u['sistema'] == '1']
    destinos = [(d, quien) for d, quien in destinos if d]
    if not destinos:
        _sin_canales()
        return 0
    return sum(enviar(d, titulo, texto, nivel, lineas, quien) for d, quien in destinos)


def a_correo(email, titulo, texto, lineas=()):
    """Un correo a una sola persona (su cuenta de la web aprobada, una invitación...). Devuelve si se ha
    enviado: sin servidor de correo, False (la web enseña entonces lo que habría que decirle)."""
    correo = _correo_config([email])
    return bool(correo) and enviar({'correo': correo}, titulo, texto, 'info', lineas, email) == 1


def _resumen(avisos, comercio=False):
    """(titulo, texto, nivel, lineas) de un mensaje con varios avisos: los críticos y más recientes primero,
    max_lineas en detalle y del resto solo el total. A un comercio no se le dice en qué otros comercios
    se usó la tarjeta: solo la tarjeta y que hay pagos sospechosos en el suyo."""
    orden = sorted(avisos, key=lambda a: str(a['dia']), reverse=True)               # los más recientes...
    orden = sorted(orden, key=lambda a: -NIVELES_AVISO.get(a['nivel'], 0))          # ...dentro de cada nivel
    maximo = int(_cfg.get('max_lineas', 10))
    criticos = sum(a['nivel'] == 'critico' for a in avisos)
    lineas = [f'{a["dia"]} [{a["nivel"].upper()}] '
              + (f'{a["mensaje"].split(":")[0]}: pagos sospechosos en tu comercio' if comercio else a['mensaje'])
              for a in orden[:maximo]]
    if len(avisos) > maximo:
        lineas.append(f'... y {len(avisos) - maximo} más: el detalle está en la web, pestaña Avisos')
    return (f'{len(avisos)} aviso{"s" if len(avisos) != 1 else ""} de fraude nuevo'
            f'{"s" if len(avisos) != 1 else ""} ({criticos} crítico{"s" if criticos != 1 else ""})',
            'Casos (misma tarjeta y día) que hay que revisar cuanto antes:', 'critico' if criticos else 'alto', lineas)


def notificar_avisos(avisos):
    """Los avisos de fraude nuevos de una ejecución (generar_avisos.py), en un solo mensaje por destino:
    los canales del servidor y la gente de la entidad reciben todos; cada comercio, solo los de sus pagos. Así la
    primera ejecución, que crea los avisos de todo el histórico, no manda cientos de mensajes."""
    if not avisos or not _cfg.get('avisos_fraude', True):
        return 0
    destinos = [(canales(), '', avisos, False)]
    for u in usuarios():
        if u['avisos_fraude'] != '1':
            continue
        comercio = u['rol'] == 'comercio'
        suyos = [a for a in avisos if u['comercio_id'] in a.get('comercios', [])] if comercio else avisos
        destinos.append((canales_de(u), u['email'], suyos, comercio))
    destinos = [d for d in destinos if d[0] and d[2]]
    if not destinos:
        _sin_canales()
        return 0
    return sum(enviar(d, *_resumen(suyos, comercio), quien=quien) for d, quien, suyos, comercio in destinos)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--prueba', action='store_true', help='envía un mensaje de prueba a los canales del servidor')
    parser.add_argument('--titulo')
    parser.add_argument('--texto', default='')
    parser.add_argument('--nivel', default='info', choices=sorted(COLOR_TEAMS))
    parser.add_argument('--tipo', choices=['avisos_fraude', 'fallos_pipeline', 'drift'])
    args = parser.parse_args()
    if args.prueba:
        print('Canales del servidor:', ', '.join(canales()) or 'ninguno')
        enviados = enviar(canales(), 'Prueba de notificaciones', 'Si lees esto, el canal está bien configurado.',
                          'info', ['Mensaje enviado con python pipeline/notificar.py --prueba'])
        sys.exit(0 if enviados or not canales() else 1)
    if not args.titulo:
        parser.error('falta --titulo (o usa --prueba)')
    notificar(args.titulo, args.texto, args.nivel, tipo=args.tipo)


if __name__ == '__main__':
    main()
