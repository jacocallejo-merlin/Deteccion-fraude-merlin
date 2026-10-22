'use strict';
// Ajustes de avisos (botón del engranaje en la cabecera): canal de Teams o Slack, correos y qué avisar.
// Pide /api/ajustes (web/ajustes.py) y usa api() y esc() de app.js, que se carga antes.

const dialogo = document.getElementById('ajustes');
const campo = (id) => document.getElementById(id);
let ajustesLeidos = null;

function mensajeAjustes(texto, error = false) {
  const p = campo('aj-mensaje');
  p.textContent = texto;
  p.classList.toggle('error-texto', error);
}

function estadoCanal(c, nombre) {
  if (!c.activo) return `Sin ${nombre}: no se envía nada por aquí.`;
  return `Activo (dirección acabada en ${c.final})${c.desde_web ? '' : ', configurado en el servidor'}. Déjalo vacío para no cambiarlo.`;
}

function pintarAjustes(a) {
  ajustesLeidos = a;
  campo('aj-teams').value = '';                 // las direcciones son secretas: no se vuelven a enseñar
  campo('aj-slack').value = '';
  campo('aj-teams-estado').textContent = estadoCanal(a.teams, 'Teams');
  campo('aj-slack-estado').textContent = estadoCanal(a.slack, 'Slack');
  campo('aj-correos').value = a.correo_destinatarios || '';
  campo('aj-correo-estado').textContent = a.correo_servidor ? ''
    : 'El servidor de correo aún no está configurado en el servidor: de momento los correos no se enviarán.';
  campo('aj-fraude').checked = a.avisos_fraude;
  campo('aj-drift').checked = a.drift;
}

async function abrirAjustes() {
  mensajeAjustes('');
  dialogo.showModal();
  try {
    pintarAjustes(await api('/api/ajustes'));
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

async function guardarAjustes() {
  const cuerpo = {
    correo_destinatarios: campo('aj-correos').value,
    avisos_fraude: campo('aj-fraude').checked,
    drift: campo('aj-drift').checked,
  };
  // Una dirección nueva solo se manda si se ha escrito algo (vacío = no cambiar)
  if (campo('aj-teams').value.trim()) cuerpo.teams_webhook_url = campo('aj-teams').value;
  if (campo('aj-slack').value.trim()) cuerpo.slack_webhook_url = campo('aj-slack').value;
  try {
    pintarAjustes(await api('/api/ajustes', {}, cuerpo));
    mensajeAjustes('Guardado. Los próximos avisos llegarán a estos canales.');
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

async function probarAjustes() {
  mensajeAjustes('Enviando…');
  try {
    const r = await api('/api/ajustes/prueba', {}, {});
    mensajeAjustes(r.enviados
      ? `Mensaje de prueba enviado a ${r.enviados} canal${r.enviados === 1 ? '' : 'es'}. Compruébalo en Teams o en tu correo.`
      : `No se ha enviado nada. ${r.detalle.join(' ')}`, !r.enviados);
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

campo('abrir-ajustes').addEventListener('click', abrirAjustes);
campo('cerrar-ajustes').addEventListener('click', () => dialogo.close());
campo('form-ajustes').addEventListener('submit', (e) => { e.preventDefault(); guardarAjustes(); });
campo('aj-probar').addEventListener('click', probarAjustes);
