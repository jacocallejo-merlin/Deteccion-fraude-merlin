'use strict';
// Mis avisos (en el menú de tu cuenta): cada usuario elige si los recibe en su correo, en un canal de Teams
// (conectando su cuenta de Microsoft) o en su chat de Teams con un flujo, y cuáles. Pide /api/ajustes (web/ajustes.py) y usa api() de app.js, que se carga antes.

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

async function abrirAjustes(aviso = '', error = false) {
  mensajeAjustes(aviso, error);
  dialogo.showModal();
  try {
    pintarAjustes(await api('/api/ajustes'));
    await pintarTeams();
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
}

// ------------------------------------------------------------------ Conectar con Microsoft Teams (web/teams.py)
async function pintarTeams() {
  const t = await api('/api/teams');
  campo('aj-ms').hidden = !t.disponible;          // sin la aplicación de Microsoft configurada, no se ofrece
  campo('aj-ms-desconectado').hidden = t.conectado;
  campo('aj-ms-conectado').hidden = !t.conectado;
  if (!t.conectado) return;
  campo('aj-ms-cuenta').textContent = t.cuenta;
  const lista = campo('aj-ms-canal');
  try {
    const { canales } = await api('/api/teams/canales');
    lista.innerHTML = '<option value="">Elige un canal…</option>' + canales.map((c) =>
      `<option value="${esc(c.equipo_id)}|${esc(c.canal_id)}" data-equipo="${esc(c.equipo)}" data-canal="${esc(c.canal)}"`
      + `${c.equipo === t.equipo && c.canal === t.canal ? ' selected' : ''}>${esc(c.equipo)} › ${esc(c.canal)}</option>`).join('');
    if (!canales.length) lista.innerHTML = '<option value="">No eres miembro de ningún equipo de Teams</option>';
  } catch (e) {
    lista.innerHTML = '<option value="">No se han podido cargar tus equipos</option>';
    mensajeAjustes(e.message, true);
  }
}

campo('aj-ms-conectar').addEventListener('click', () => { location.href = '/api/teams/conectar'; });
campo('aj-ms-canal').addEventListener('change', async (e) => {
  const opcion = e.target.selectedOptions[0];
  if (!opcion.value) return;
  const [equipo_id, canal_id] = opcion.value.split('|');
  try {
    await api('/api/teams/canal', {}, { equipo_id, canal_id, equipo: opcion.dataset.equipo, canal: opcion.dataset.canal });
    mensajeAjustes(`Listo: los avisos se publicarán en ${opcion.dataset.equipo} › ${opcion.dataset.canal}. Pruébalo con «Enviar mensaje de prueba».`);
  } catch (err) {
    mensajeAjustes(err.message, true);
  }
});
campo('aj-ms-desconectar').addEventListener('click', async () => {
  try {
    await api('/api/teams/desconectar', {}, {});
    await pintarTeams();
    mensajeAjustes('Microsoft Teams desconectado: ya no se publicará nada en tu canal.');
  } catch (e) {
    mensajeAjustes(e.message, true);
  }
});

// Al volver de iniciar sesión en Microsoft (?teams=elegir o ?teams=error): abrir Mis avisos para seguir
{
  const vuelta = new URLSearchParams(location.search);
  if (vuelta.get('teams')) {
    history.replaceState(null, '', location.pathname);
    if (vuelta.get('teams') === 'elegir') abrirAjustes('Conectado con Microsoft Teams. Ahora elige el canal.');
    else abrirAjustes(`No se ha podido conectar con Microsoft Teams: ${vuelta.get('motivo') || 'error desconocido'}`, true);
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

campo('abrir-ajustes').addEventListener('click', () => abrirAjustes());
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
