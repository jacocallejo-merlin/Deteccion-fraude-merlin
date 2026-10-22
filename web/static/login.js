'use strict';
// Pantalla de entrada (web/sesiones.py y web/cuentas.py): entrar, contraseña olvidada (código de 6 cifras por
// correo), registro de un comercio (con código para confirmar el correo) y cambio de la contraseña provisional.
// Al entrar, vuelve a la página que se quería abrir (?volver=...), solo si es de esta misma máquina.

const $ = (id) => document.getElementById(id);
const parametros = new URLSearchParams(location.search);
let provisional = '';            // la contraseña con la que se acaba de entrar, si hay que cambiarla

function destino() {
  try {
    const url = new URL(parametros.get('volver'), location.origin);
    if (url.hostname === location.hostname && !url.pathname.startsWith('/login')) return url.href;
  } catch (e) { /* sin volver o no válido: a la portada */ }
  return '/';
}

async function enviar(ruta, cuerpo) {
  const r = await fetch(ruta, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cuerpo) });
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof datos.detail === 'string' ? datos.detail : 'Revisa los datos del formulario');
  return datos;
}

function verVista(id) {
  document.querySelectorAll('.vista').forEach((v) => { v.hidden = v.id !== id; });
  document.querySelectorAll(`#${id} .error-texto`).forEach((m) => { m.textContent = ''; });
  [...document.querySelectorAll(`#${id} input`)].find((i) => !i.closest('[hidden]') && !i.value)?.focus();
}
document.querySelectorAll('[data-vista]').forEach((b) => b.addEventListener('click', () => verVista(b.dataset.vista)));

// Sin servidor de correo y en este mismo equipo, el servidor devuelve el código para poder probar
function codigoDemo(id, r) {
  $(id).hidden = !r.codigo_demo;
  $(id).innerHTML = r.codigo_demo
    ? `Modo de prueba (aún no hay servidor de correo): tu código es <b>${r.codigo_demo}</b>` : '';
}

function alEnviar(formulario, mensaje, accion) {
  $(formulario).addEventListener('submit', async (e) => {
    e.preventDefault();
    const boton = e.target.querySelector('button[type=submit]');
    $(mensaje).textContent = '';
    boton.disabled = true;
    try {
      await accion();
    } catch (err) {
      $(mensaje).textContent = err.message;
    }
    boton.disabled = false;
  });
}

function iguales(a, b) {
  if ($(a).value !== $(b).value) throw new Error('Las contraseñas no coinciden');
}

// ------------------------------------------------------------------ entrar
alEnviar('form-entrar', 'm-entrar', async () => {
  const r = await enviar('/api/login', { email: $('email').value, contrasena: $('contrasena').value, recordar: $('recordar').checked });
  if (r.cambiar_contrasena) {
    provisional = $('contrasena').value;
    $('k-actual').value = provisional;          // ya la ha escrito: no se vuelve a pedir
    $('zona-provisional').hidden = true;
    verVista('vista-cambiar');
    return;
  }
  location.href = destino();
});

// ------------------------------------------------------------------ contraseña olvidada
alEnviar('form-olvido', 'm-olvido', async () => {
  const r = await enviar('/api/recuperar', { email: $('o-email').value });
  $('nueva-texto').textContent = `Si hay una cuenta con ${$('o-email').value}, te acabamos de mandar un código de 6 cifras `
    + '(mira también en el correo no deseado). Vale 15 minutos.';
  codigoDemo('nueva-demo', r);
  verVista('vista-nueva');
});

alEnviar('form-nueva', 'm-nueva', async () => {
  iguales('n-contrasena', 'n-repetida');
  await enviar('/api/recuperar/confirmar', { email: $('o-email').value, codigo: $('n-codigo').value, contrasena: $('n-contrasena').value });
  location.href = destino();
});

// ------------------------------------------------------------------ registro de un comercio
alEnviar('form-registro', 'm-registro', async () => {
  iguales('r-contrasena', 'r-repetida');
  const r = await enviar('/api/registro', {
    empresa: $('r-empresa').value, nombre: $('r-nombre').value, email: $('r-email').value, contrasena: $('r-contrasena').value,
  });
  $('confirmar-texto').textContent = `Te hemos mandado un código de 6 cifras a ${$('r-email').value}. Vale 15 minutos.`;
  codigoDemo('confirmar-demo', r);
  verVista('vista-confirmar');
});

alEnviar('form-confirmar', 'm-confirmar', async () => {
  await enviar('/api/registro/confirmar', { email: $('r-email').value, codigo: $('c-codigo').value });
  location.href = '/';           // la primera vez, la web le pide configurar su empresa
});

// ------------------------------------------------------------------ cambiar la contraseña provisional
alEnviar('form-cambiar', 'm-cambiar', async () => {
  iguales('k-nueva', 'k-repetida');
  await enviar('/api/perfil/contrasena', { actual: provisional || $('k-actual').value, nueva: $('k-nueva').value });
  location.href = destino();
});

// Llegar con una sesión que aún tiene la contraseña provisional (la web manda aquí con ?cambiar=1)
if (parametros.get('cambiar')) verVista('vista-cambiar');
