'use strict';
// Pantalla de entrada (web/sesiones.py y web/cuentas.py): entrar, contraseña olvidada (código de 6 cifras por
// correo), registro de un comercio (con código para confirmar el correo) y cambio de la contraseña provisional.
// Al entrar, vuelve a la página que se quería abrir (?volver=...), solo si es de esta misma máquina.

const $ = (id) => document.getElementById(id);
const parametros = new URLSearchParams(location.search);
let provisional = '';            // la contraseña con la que se acaba de entrar, si hay que cambiarla

function destino() {
  const volver = parametros.get('volver');
  if (!volver) return '/';
  try {
    const url = new URL(volver, location.origin);
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

// ------------------------------------------------------------------ registro de un comercio (3 pasos)
const CAMPOS_EMPRESA = ['nombre', 'cif', 'categoria', 'pais', 'ciudad', 'direccion', 'telefono', 'web'];
const cuenta = () => ({ nombre: $('r-nombre').value, email: $('r-email').value, contrasena: $('r-contrasena').value });

// Paso 1: la cuenta. El servidor comprueba ya el correo y la contraseña, antes de pedir la empresa
alEnviar('form-registro', 'm-registro', async () => {
  iguales('r-contrasena', 'r-repetida');
  await enviar('/api/registro/comprobar', cuenta());
  verVista('vista-registro-empresa');
});

// Paso 2: la empresa y su tamaño (cuántos pagos al día le llegarán). Con eso se manda el código
async function cargarOpciones() {
  if ($('re-tamanos').children.length) return;
  const o = await fetch('/api/registro/opciones').then((r) => r.json());
  $('re-sectores').innerHTML = o.sectores.map((s) => `<option value="${escapar(s)}">`).join('');
  $('re-tamanos').innerHTML = o.tamanos.map((t, i) => `
    <label class="tamano"><input type="radio" name="tamano" value="${t.clave}" ${i === 0 ? 'checked' : ''}>
      <b>${escapar(t.nombre)}</b><span class="sub">unos ${t.pagos_dia} pagos al día</span></label>`).join('');
}
document.querySelector('[data-vista="vista-registro"]').addEventListener('click', () => cargarOpciones().catch(() => {}));

alEnviar('form-registro-empresa', 'm-registro-empresa', async () => {
  await cargarOpciones();
  const empresa = Object.fromEntries(CAMPOS_EMPRESA.map((c) => [c, $(`re-${c}`).value]));
  const tamano = document.querySelector('input[name=tamano]:checked')?.value || 'pequeno';
  const r = await enviar('/api/registro', { ...cuenta(), empresa, tamano });
  $('confirmar-texto').textContent = `Te hemos mandado un código de 6 cifras a ${$('r-email').value} `
    + '(mira también en el correo no deseado). Vale 15 minutos.';
  codigoDemo('confirmar-demo', r);
  verVista('vista-confirmar');
});

// Paso 3: el código. Se crean la cuenta y la empresa, y entra
alEnviar('form-confirmar', 'm-confirmar', async () => {
  await enviar('/api/registro/confirmar', { email: $('r-email').value, codigo: $('c-codigo').value });
  location.href = '/';
});

// ------------------------------------------------------------------ cambiar la contraseña provisional
alEnviar('form-cambiar', 'm-cambiar', async () => {
  iguales('k-nueva', 'k-repetida');
  await enviar('/api/perfil/contrasena', { actual: provisional || $('k-actual').value, nueva: $('k-nueva').value });
  location.href = destino();
});

// ------------------------------------------------------------------ entrar como invitado (solo mirar)
fetch('/api/invitado').then((r) => r.json()).then((d) => { $('zona-invitado').hidden = !d.disponible; }).catch(() => {});
document.querySelectorAll('[data-invitado]').forEach((b) => b.addEventListener('click', async () => {
  try {
    await enviar('/api/invitado', { rol: b.dataset.invitado });
    location.href = destino();
  } catch (err) {
    $('m-entrar').textContent = err.message;
  }
}));

// El invitado de comercio elige antes qué empresa ver, por nombre o por sector
let empresas = [];
const escapar = (t) => String(t ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function pintarEmpresas() {
  const texto = $('ie-buscar').value.trim().toLowerCase();
  const sector = $('ie-sector').value;
  const vistas = empresas.filter((e) => (!sector || e.categoria === sector) && e.nombre.toLowerCase().includes(texto));
  $('ie-lista').innerHTML = vistas.length ? vistas.map((e) => `
    <button type="button" class="empresa-opcion" role="listitem" data-comercio="${e.id}">
      <b>${escapar(e.nombre)}</b>
      <span class="sub">${escapar([e.categoria, e.ciudad].filter(Boolean).join(' · '))}${e.pagos ? ` · ${e.pagos.toLocaleString('es-ES')} pagos` : ' · sin pagos aún'}</span>
    </button>`).join('') : '<p class="sub">No hay ninguna empresa con ese nombre en ese sector.</p>';
}

document.querySelector('[data-vista="vista-invitado-comercio"]').addEventListener('click', async () => {
  if (empresas.length) return;
  try {
    const r = await fetch('/api/invitado/comercios');
    empresas = await r.json();
    const sectores = [...new Set(empresas.map((e) => e.categoria).filter(Boolean))].sort();
    $('ie-sector').innerHTML = '<option value="">Todos los sectores</option>'
      + sectores.map((s) => `<option>${escapar(s)}</option>`).join('');
    pintarEmpresas();
  } catch (err) {
    $('m-invitado').textContent = 'No se ha podido cargar la lista de empresas';
  }
});
$('ie-buscar').addEventListener('input', pintarEmpresas);
$('ie-sector').addEventListener('change', pintarEmpresas);
$('ie-lista').addEventListener('click', async (e) => {
  const b = e.target.closest('[data-comercio]');
  if (!b) return;
  try {
    await enviar('/api/invitado', { rol: 'comercio', comercio_id: Number(b.dataset.comercio) });
    location.href = '/';
  } catch (err) {
    $('m-invitado').textContent = err.message;
  }
});

// Llegar con una sesión que aún tiene la contraseña provisional (la web manda aquí con ?cambiar=1)
if (parametros.get('cambiar')) verVista('vista-cambiar');
