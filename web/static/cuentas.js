'use strict';
// Pestaña Cuentas (solo el administrador de la entidad): dar cuentas a su equipo y a los comercios que ya
// trabajan con la entidad (con una contraseña provisional que la persona cambia al entrar), desactivarlas,
// reactivarlas y darles una contraseña provisional nueva. Pide /api/cuentas (web/cuentas.py) y usa api(),
// esc(), estado, CARGADORES y CONTENEDOR_ERROR de app.js.

const ROL_TEXTO = { administrador: 'Administrador', analista: 'Analista' };
const ct = (id) => document.getElementById(id);

function provisional(email, contrasena, que) {
  ct('c-mensaje').classList.remove('error-texto');
  ct('c-mensaje').innerHTML = `${que}. Dale a <b>${esc(email)}</b> esta contraseña provisional (solo se enseña ahora):
    <input class="enlace-invitacion" readonly value="${esc(contrasena)}" onclick="this.select()">
    La primera vez que entre tendrá que poner una suya.`;
  ct('c-mensaje').scrollIntoView({ block: 'center' });
}

function filaCuenta(c) {
  const yo = c.email === estado.sesion?.email;
  const activa = c.estado === 'activa';
  const acciones = yo ? '<span class="sub">Tú</span>' : `
    <button class="enlace" data-cuenta="${esc(c.email)}" data-accion="${activa ? 'desactivar' : 'reactivar'}">
      ${activa ? 'Desactivar' : 'Reactivar'}</button>
    ${activa ? `<button class="enlace" data-cuenta="${esc(c.email)}" data-accion="contrasena_provisional">Nueva contraseña</button>` : ''}`;
  const pendiente = activa && c.cambiar_contrasena ? '<div class="sub">Aún no ha entrado</div>' : '';
  const como = c.revisada_por === 'registro' ? ' <span class="sub">(se registró)</span>' : '';
  return `<tr class="${activa ? '' : 'apagada'}">
    <td><b>${esc(c.nombre)}</b><div class="sub">${esc(c.email)}</div></td>
    <td>${ROL_TEXTO[c.rol] || `Comercio: ${esc(c.comercio || c.comercio_id)}${como}`}</td>
    <td>${activa ? 'Activa' : 'Desactivada'}${pendiente}</td>
    <td class="acciones-cuenta">${acciones}</td>
  </tr>`;
}

async function cargarCuentas() {
  const d = await api('/api/cuentas');
  ct('t-cuentas').innerHTML = `<table>
    <thead><tr><th>Persona</th><th>Ve</th><th>Estado</th><th></th></tr></thead>
    <tbody>${d.cuentas.map(filaCuenta).join('')}</tbody></table>`;
}

CARGADORES.cuentas = cargarCuentas;
CONTENEDOR_ERROR.cuentas = 't-cuentas';

ct('c-rol').addEventListener('change', (e) => {
  ct('c-comercio').hidden = e.target.value !== 'comercio';
  ct('c-comercio').required = !ct('c-comercio').hidden;
  ct('c-nombre').placeholder = ct('c-comercio').hidden ? 'Nombre de la persona' : 'Nombre de la persona (opcional)';
});

ct('form-crear').addEventListener('submit', async (e) => {
  e.preventDefault();
  const rol = ct('c-rol').value;
  try {
    const r = await api('/api/cuentas/crear', {}, {
      rol, nombre: ct('c-nombre').value, email: ct('c-email').value,
      comercio_id: rol === 'comercio' ? Number(ct('c-comercio').value) : 0,
    });
    ['c-nombre', 'c-email', 'c-comercio'].forEach((id) => { ct(id).value = ''; });
    provisional(r.email, r.contrasena_provisional, 'Cuenta creada');
    await cargarCuentas();
  } catch (err) {
    ct('c-mensaje').classList.add('error-texto');
    ct('c-mensaje').textContent = err.message;
  }
});

ct('t-cuentas').addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-accion]');
  if (!b) return;
  const cuerpo = { email: b.dataset.cuenta, accion: b.dataset.accion };
  if (cuerpo.accion === 'desactivar' && !window.confirm(`${cuerpo.email} ya no podrá entrar. ¿Desactivar la cuenta?`)) return;
  if (cuerpo.accion === 'contrasena_provisional'
      && !window.confirm(`Su contraseña actual dejará de valer y se cerrará su sesión. ¿Dar a ${cuerpo.email} una provisional nueva?`)) return;
  b.disabled = true;
  try {
    const r = await api('/api/cuentas/estado', {}, cuerpo);
    if (r.contrasena_provisional) provisional(r.email, r.contrasena_provisional, 'Contraseña cambiada');
    await cargarCuentas();
  } catch (err) {
    window.alert(err.message);
    b.disabled = false;
  }
});
