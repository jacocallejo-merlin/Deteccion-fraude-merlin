'use strict';
// Mis avisos (botón del engranaje en la cabecera): cada usuario elige si los recibe en su correo, en su Teams
// (su chat o un canal) y cuáles. Pide /api/ajustes (web/ajustes.py) y usa api() de app.js, que se carga antes.

const dialogo = document.getElementById('ajustes');
const campo = (id) => document.getElementById(id);
let ajustesLeidos = null;

function mensajeAjustes(texto, error = false) {
  const p = campo('aj-mensaje');
  p.textContent = texto;
  p.classList.toggle('error-texto', error);
}

function estadoTeams(c) {
  return c.activo ? `Activo (dirección acabada en ${c.final}). Déjalo vacío para no cambiarlo.` : '';
}

function pintarAjustes(a) {
  ajustesLeidos = a;
  campo('aj-teams').value = '';                 // la dirección es secreta: no se vuelve a enseñar
  campo('aj-teams-estado').textContent = estadoTeams(a.teams);
  campo('aj-quitar-teams').hidden = !a.teams.activo;
  campo('aj-email').textContent = a.email;
  campo('aj-correo').checked = a.correo;
  campo('aj-correo-estado').textContent = a.correo_servidor ? ''
    : 'El servidor de correo aún no está configurado en el servidor: de momento los correos no se enviarán.';
  campo('aj-fraude').checked = a.avisos_fraude;
  // Los avisos del sistema (drift, fallos del pipeline) solo son para los analistas
  campo('aj-sistema-zona').hidden = a.sistema === null;
  campo('aj-sistema').checked = Boolean(a.sistema);
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
  const cuerpo = { correo: campo('aj-correo').checked, avisos_fraude: campo('aj-fraude').checked };
  if (ajustesLeidos && ajustesLeidos.sistema !== null) cuerpo.sistema = campo('aj-sistema').checked;
  // Una dirección nueva solo se manda si se ha escrito algo (vacío = no cambiar)
  if (campo('aj-teams').value.trim()) cuerpo.teams_webhook_url = campo('aj-teams').value;
  try {
    pintarAjustes(await api('/api/ajustes', {}, cuerpo));
    mensajeAjustes('Guardado. Los próximos avisos te llegarán así.');
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

async function probarAjustes() {
  mensajeAjustes('Enviando…');
  try {
    const r = await api('/api/ajustes/prueba', {}, {});
    mensajeAjustes(r.enviados
      ? `Mensaje de prueba enviado${r.enviados > 1 ? ` a ${r.enviados} sitios` : ''}. Compruébalo en Teams o en tu correo.`
      : `No se ha enviado nada. ${r.detalle.join(' ')}`, !r.enviados);
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

campo('abrir-ajustes').addEventListener('click', abrirAjustes);
campo('cerrar-ajustes').addEventListener('click', () => dialogo.close());
campo('form-ajustes').addEventListener('submit', (e) => { e.preventDefault(); guardarAjustes(); });
campo('aj-probar').addEventListener('click', probarAjustes);
campo('aj-quitar-teams').addEventListener('click', async () => {
  try {
    pintarAjustes(await api('/api/ajustes', {}, { teams_webhook_url: '' }));
    mensajeAjustes('Ya no recibirás los avisos en Teams.');
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
});
