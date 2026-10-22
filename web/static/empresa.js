'use strict';
// Mi empresa (solo cuentas de comercio): los datos de la empresa. La primera vez que entra un comercio se abre
// sola, como "Configura tu empresa". Pide /api/empresa (web/cuentas.py) y usa api() de app.js.

const em = (id) => document.getElementById(id);
const CAMPOS = ['nombre', 'cif', 'categoria', 'pais', 'ciudad', 'direccion', 'telefono', 'web'];

function pintarEmpresa(e) {
  CAMPOS.forEach((c) => { em(`em-${c}`).value = e[c] || ''; });
  em('em-sectores').innerHTML = e.sectores.map((s) => `<option value="${esc(s)}">`).join('');
  em('empresa-titulo').textContent = e.configurada ? 'Mi empresa' : 'Configura tu empresa';
  const vacio = e.pagos ? '' : ' Aún no hay pagos tuyos: aparecerán aquí en cuanto tu entidad empiece a enviarlos.';
  em('em-intro').textContent = e.configurada ? `Comercio número ${e.comercio_id}.${vacio}`
    : `Bienvenido a Centinela. Antes de empezar, completa los datos de tu empresa (puedes cambiarlos cuando quieras desde tu perfil).${vacio}`;
}

async function abrirEmpresa() {
  em('em-mensaje').textContent = '';
  try {
    pintarEmpresa(await api('/api/empresa'));
    em('empresa').showModal();
  } catch (e) {
    window.alert(e.message);
  }
}

em('abrir-empresa').addEventListener('click', abrirEmpresa);
em('cerrar-empresa').addEventListener('click', () => em('empresa').close());
em('form-empresa').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    const cuerpo = Object.fromEntries(CAMPOS.map((c) => [c, em(`em-${c}`).value]));
    const primeraVez = em('empresa-titulo').textContent !== 'Mi empresa';
    pintarEmpresa(await api('/api/empresa', {}, cuerpo));
    em('em-mensaje').textContent = 'Guardado.';
    if (primeraVez) setTimeout(() => location.reload(), 600);     // el nombre nuevo, también en la cabecera
  } catch (e) {
    em('em-mensaje').textContent = e.message;
  }
});

// Solo para los comercios: el botón del menú y, si aún no la ha configurado, abrirla al entrar
api('/api/sesion').then(async (s) => {
  if (s.rol !== 'comercio') return;
  em('abrir-empresa').hidden = false;
  const e = await api('/api/empresa');
  if (!e.configurada) {
    pintarEmpresa(e);
    em('empresa').showModal();
  }
}).catch(() => {});
