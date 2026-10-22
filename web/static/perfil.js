'use strict';
// El botón de tu cuenta (arriba a la derecha, con tus iniciales): un menú con Mi perfil, Mis avisos
// (ajustes.js) y Cerrar sesión (app.js), y el diálogo Mi perfil: nombre, teléfono, contraseña y sesiones
// abiertas. Pide /api/perfil (web/sesiones.py) y usa api() y estado de app.js, que se carga antes.

const pf = (id) => document.getElementById(id);
const menuPerfil = pf('menu-perfil');

function iniciales(nombre) {
  const partes = String(nombre || '?').trim().split(/\s+/);
  return (partes[0][0] + (partes.length > 1 ? partes[partes.length - 1][0] : '')).toUpperCase();
}

function pintarCabecera(p) {
  pf('perfil-iniciales').textContent = iniciales(p.nombre);
  pf('sesion-nombre').textContent = p.nombre;
  pf('menu-email').textContent = p.email;
  pf('abrir-menu-perfil').title = `${p.nombre} (${p.email})`;
}

function mensaje(id, texto, error = false) {
  pf(id).textContent = texto;
  pf(id).classList.toggle('error-texto', error);
}

function pintarPerfil(p) {
  pintarCabecera(p);
  pf('pf-nombre').value = p.nombre;
  pf('pf-telefono').value = p.telefono || '';
  const ve = {
    administrador: 'Administrador de la entidad: ves todos los comercios y das las cuentas.',
    analista: 'Analista de la entidad: ves todos los comercios.',
  }[p.rol] || `Cuenta del comercio ${p.comercio} (número ${p.comercio_id}): ves solo sus pagos.`;
  pf('pf-cuenta').textContent = `${p.email}. ${ve} Para cambiar el correo, habla con el administrador de tu entidad.`;
  pf('pf-sesiones').textContent = p.otras_sesiones > 0
    ? `Además de en este equipo, tienes la sesión abierta en ${p.otras_sesiones === 1 ? 'otro' : `otros ${p.otras_sesiones}`}.`
    : 'Solo tienes la sesión abierta en este equipo.';
  pf('pf-cerrar-otras').hidden = p.otras_sesiones <= 0;
}

// ------------------------------------------------------------------ el menú
function abrirMenu(abierto) {
  menuPerfil.hidden = !abierto;
  pf('abrir-menu-perfil').setAttribute('aria-expanded', String(abierto));
}
pf('abrir-menu-perfil').addEventListener('click', () => abrirMenu(menuPerfil.hidden));
menuPerfil.addEventListener('click', (e) => { if (e.target.closest('button')) abrirMenu(false); });
document.addEventListener('click', (e) => {
  if (!e.target.closest('#menu-perfil, #abrir-menu-perfil')) abrirMenu(false);
});
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') abrirMenu(false); });

// ------------------------------------------------------------------ Mi perfil
pf('abrir-perfil').addEventListener('click', async () => {
  ['pf-mensaje', 'pf-mensaje-contrasena'].forEach((id) => mensaje(id, ''));
  pf('form-contrasena').reset();
  pf('perfil').showModal();
  try {
    pintarPerfil(await api('/api/perfil'));
  } catch (e) {
    mensaje('pf-mensaje', e.message, true);
  }
});
pf('cerrar-perfil').addEventListener('click', () => pf('perfil').close());

pf('form-perfil').addEventListener('submit', async (e) => {
  e.preventDefault();
  try {
    const p = await api('/api/perfil', {}, { nombre: pf('pf-nombre').value, telefono: pf('pf-telefono').value });
    pintarPerfil(p);
    estado.usuario = p.nombre;              // los veredictos de ahora en adelante, con el nombre nuevo
    mensaje('pf-mensaje', 'Guardado.');
  } catch (err) {
    mensaje('pf-mensaje', err.message, true);
  }
});

pf('form-contrasena').addEventListener('submit', async (e) => {
  e.preventDefault();
  if (pf('pf-nueva').value !== pf('pf-repetida').value) {
    mensaje('pf-mensaje-contrasena', 'Las contraseñas nuevas no coinciden', true);
    return;
  }
  try {
    const r = await api('/api/perfil/contrasena', {}, { actual: pf('pf-actual').value, nueva: pf('pf-nueva').value });
    e.target.reset();
    mensaje('pf-mensaje-contrasena', `Contraseña cambiada.${r.cerradas ? ` Se ha cerrado la sesión en ${r.cerradas === 1 ? 'otro equipo' : `${r.cerradas} equipos más`}.` : ''}`);
    pintarPerfil(await api('/api/perfil'));
  } catch (err) {
    mensaje('pf-mensaje-contrasena', err.message, true);
  }
});

pf('pf-cerrar-otras').addEventListener('click', async () => {
  try {
    const r = await api('/api/perfil/cerrar-otras', {}, {});
    pintarPerfil(await api('/api/perfil'));
    pf('pf-sesiones').textContent = `Hecho: se ha cerrado la sesión en ${r.cerradas === 1 ? 'otro equipo' : `${r.cerradas} equipos`}.`;
  } catch (err) {
    pf('pf-sesiones').textContent = err.message;
  }
});

// Las iniciales y el correo en la cabecera, al cargar la página
api('/api/perfil').then(pintarCabecera).catch(() => {});
