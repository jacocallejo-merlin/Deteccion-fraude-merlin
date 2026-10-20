'use strict';
// Centinela: el parte de la noche, el registro de casos, el resumen, el buscador de pagos y los avisos.
// Pide los datos a web/app.py.

const COLOR = { critico: '#D33A2C', alto: '#E0952B', medio: '#C9A400', fraude: '#D33A2C', legitima: '#0B7A64',
  pendiente: '#2E4A52', vigilancia: '#D8DDD6', sin_incidencia: '#B4BDB9', tinta: '#18211F', linea: '#DCE0DA',
  suave: '#5E6A66' };
const NIVEL_TEXTO = { critico: 'Crítico', alto: 'Alto', medio: 'Medio' };
const NIVEL_GRUPO = { critico: 'Críticos', alto: 'Altos', medio: 'Medios' };
const NIVEL_PLURAL = { critico: 'críticos', alto: 'altos', medio: 'medios' };
const NIVEL_DE = { 3: 'critico', 2: 'alto', 1: 'medio' };
const ESTADO_TEXTO = { pendiente: 'Por resolver', vigilancia: 'En vigilancia', fraude: 'Fraude', legitima: 'Legítima',
  sin_incidencia: 'Sin incidencia' };
const RESULTADO = { fraude: 'Era fraude', legitima: 'Era una compra legítima', sin_incidencia: 'Cerrado sin incidencia' };
const ABIERTOS = ['pendiente', 'vigilancia'];
const REFRESCO_MS = 60000;

Chart.defaults.font.family = '"Schibsted Grotesk", "Segoe UI", system-ui, sans-serif';
Chart.defaults.font.size = 12.5;
Chart.defaults.color = COLOR.suave;
Chart.defaults.borderColor = COLOR.linea;
Chart.defaults.plugins.legend.labels.boxWidth = 10;
Chart.defaults.plugins.legend.labels.boxHeight = 10;
Chart.defaults.maintainAspectRatio = false;

const leer = (clave, defecto) => { try { return localStorage.getItem(clave) ?? defecto; } catch { return defecto; } };
const guardar = (clave, valor) => { try { localStorage.setItem(clave, valor); } catch { /* sin almacenamiento */ } };

const estado = {
  pestana: 'alertas', comercio: leer('comercio', '0'), filtroEstado: 'pendientes', filtroAvisos: 'no_leidos',
  usuario: leer('usuario', 'Analista'), demo: leer('demo', '0') === '1', comercios: {},
  noLeidos: null, caso: null, niveles: ['critico', 'alto'],
  ordenAlertas: leer('ordenAlertas', 'riesgo'), ordenTransacciones: leer('ordenTransacciones', 'reciente'),
};
const graficas = {};

// ------------------------------------------------------------------------------ formato
const nf = new Intl.NumberFormat('es-ES');
const num = (n) => (n == null ? '–' : nf.format(n));
const eur = (n) => (n == null ? '–' : n.toLocaleString('es-ES', { style: 'currency', currency: 'EUR' }));
const pct = (x) => (x == null || !isFinite(x) ? '–' : `${Math.round(x * 100)} %`);
const dos = (n) => String(n).padStart(2, '0');
const fecha = (s) => new Date(`${String(s).slice(0, 10)}T00:00:00`);
const fechaHora = (s) => { const d = new Date(s); return `${dos(d.getDate())}/${dos(d.getMonth() + 1)}, ${dos(d.getHours())}:${dos(d.getMinutes())}`; };
const hora = (s) => { const d = new Date(s); return `${dos(d.getHours())}:${dos(d.getMinutes())}`; };
const fechaLarga = (s) => fecha(s).toLocaleDateString('es-ES', { day: 'numeric', month: 'long', year: 'numeric' });
const diaCorto = (s) => fecha(s).toLocaleDateString('es-ES', { day: 'numeric', month: 'short' }).replace('.', '');
const capital = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const TIPO_TARJETA = { debito: 'Débito', credito: 'Crédito', prepago: 'Prepago' };
const tipoTarjeta = (t) => TIPO_TARJETA[t] || capital(t || '');
const tarjeta = (t) => (t ? `•••• ${esc(String(t).slice(-4))}` : 'Tarjeta');
const plural = (n, uno, varios) => `${num(n)} ${n === 1 ? uno : varios}`;
const nivelPill = (n) => (n ? `<span class="pill ${n}">${NIVEL_TEXTO[n]}</span>` : '');
const estadoPill = (e, detalle = '') => (e ? `<span class="pill ${e}" title="${esc(detalle)}">${ESTADO_TEXTO[e]}</span>` : '');
const abierto = (e) => ABIERTOS.includes(e);
// Siempre "de cada 10", para que los niveles se comparen de un vistazo (0,63 -> 6 de cada 10). Solo por
// debajo del 10 % se pasa a "1 de cada N"
const sobreDiez = (p) => Math.min(Math.round(p * 10), p < 1 ? 9 : 10);
const proporcion = (p) => (p == null ? '' : p >= 0.1 ? `${sobreDiez(p)} de cada 10` : `1 de cada ${Math.round(1 / p)}`);
const fraseFraude = (p) => (p == null ? '' : `${proporcion(p)} ${p >= 0.15 ? 'son' : 'es'} fraude`);
// Puesto del pago entre los del día (solo si está entre el 3 % de más riesgo): 0,0015 -> "el 0,2 % más sospechoso"
const puesto = (x) => (x == null || x > 0.03 ? ''
  : `El ${Math.max(x * 100, 0.1).toLocaleString('es-ES', { maximumFractionDigits: 1 })} % más sospechoso del día`);
const realPill = (r) => (estado.demo && r ? `<span class="pill real" title="Verdad del simulador (solo en modo demostración)">Real: ${esc(Array.isArray(r) ? r.join(', ') : r)}</span>` : '');

// ------------------------------------------------------------------------------ API
async function api(ruta, parametros = {}, cuerpo = null) {
  const url = new URL(ruta, window.location.origin);
  Object.entries(parametros).forEach(([k, v]) => url.searchParams.set(k, v));
  if (estado.demo) url.searchParams.set('demo', 'true');
  const opciones = cuerpo ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cuerpo) } : {};
  const r = await fetch(url, opciones);
  const datos = await r.json().catch(() => ({}));
  if (!r.ok) {
    const detalle = Array.isArray(datos.detail) ? datos.detail.map((d) => d.msg).join('; ') : datos.detail;
    throw new Error(detalle || `Error ${r.status}`);
  }
  return datos;
}
const comercio = () => estado.comercio;
const nombreComercio = () => estado.comercios[comercio()];

function grafica(id, config) {
  if (graficas[id]) graficas[id].destroy();
  graficas[id] = new Chart(document.getElementById(id), config);
}

function mostrarError(id, e) {
  document.getElementById(id).innerHTML = `<div class="error">${esc(e.message)}</div>`;
}

function flotante(titulo, texto, alPulsar) {
  const caja = document.createElement('div');
  caja.className = 'flotante';
  caja.innerHTML = `<b>${esc(titulo)}</b>${esc(texto)}`;
  caja.addEventListener('click', () => { caja.remove(); if (alPulsar) alPulsar(); });
  document.getElementById('avisos-flotantes').append(caja);
  setTimeout(() => caja.remove(), 12000);
}

// ------------------------------------------------------------------------------ contexto
async function cargarContexto() {
  const c = await api('/api/contexto');
  estado.contexto = c;
  document.getElementById('reloj').textContent =
    `Datos hasta el ${fechaLarga(c.hoy)}. En marcha desde el ${fechaLarga(c.inicio)}.`;
  // El nivel medio es opcional (config.yaml): sin él, no hay filtro para verlo
  estado.niveles = c.niveles;
  if (!c.niveles.includes('medio')) document.getElementById('opcion-medios')?.remove();
  const sel = document.getElementById('comercio');
  sel.innerHTML = '<option value="0">Todos los comercios</option>'
    + c.comercios.map((x) => `<option value="${x.id}">${esc(x.nombre)}</option>`).join('');
  c.comercios.forEach((x) => { estado.comercios[x.id] = x.nombre; });
  if (!estado.comercios[estado.comercio]) estado.comercio = '0';
  sel.value = estado.comercio;
}

// ------------------------------------------------------------------------------ el parte de la noche
function pintarParte(r, d) {
  const criticos = r.por_nivel.critico || 0;
  const altos = r.por_nivel.alto || 0;
  document.getElementById('parte-fecha').textContent =
    capital(fecha(d.dia).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }));
  const quien = comercio() === '0' ? '' : ` de ${nombreComercio()}`;
  document.getElementById('parte-revisados').textContent = d.pagos
    ? `${plural(d.pagos, 'pago', 'pagos')}${quien} revisados esta noche.`
    : `Esta noche no ha habido pagos${quien}.`;
  const partes = [criticos && plural(criticos, 'crítico', 'críticos'), altos && plural(altos, 'alto', 'altos')].filter(Boolean);
  document.getElementById('parte-pendientes').textContent = partes.length
    ? `${partes.join(' y ')} ${criticos + altos === 1 ? 'espera' : 'esperan'} una decisión.`
    : 'Nada espera una decisión.';
  pintarDia(d);
}

// El día en una línea: una barra por pago, de menos a más riesgo; las alertas, en su color
function pintarDia(d) {
  const cont = document.getElementById('dia');
  document.getElementById('dia-ejes').hidden = !d.barras.length;
  if (!d.barras.length) { cont.innerHTML = ''; return; }
  // Cada pago ocupa un hueco; los que tienen alerta, ANCHO_ALERTA huecos, para que se vean y se puedan
  // pulsar. Con pocos pagos (vista de un comercio) las barras no se ensanchan: se pegan a la derecha
  const ANCHO_ALERTA = 5;
  const anchos = d.barras.map(([, prioridad]) => (prioridad >= 2 ? ANCHO_ALERTA : 1));
  const usados = anchos.reduce((s, a) => s + a, 0);
  const huecos = Math.max(usados, 160);
  let x = huecos - usados;
  const barras = d.barras.map(([h, prioridad, metodo, importe], i) => {
    const alto = 4 + h * 96;
    const nivel = prioridad >= 2 ? NIVEL_DE[prioridad] : '';
    const ancho = anchos[i];
    const atributos = nivel
      ? ` class="${nivel}" data-metodo="${metodo}" data-dia="${d.dia}" tabindex="0" role="button" aria-label="${NIVEL_TEXTO[nivel]}, ${eur(importe)}. Abrir el caso"`
      : '';
    const rect = `<rect x="${x + ancho * 0.16}" y="${100 - alto}" width="${ancho * 0.68}" height="${alto}" style="--i:${i}"${atributos}>`
      + `${nivel ? `<title>${NIVEL_TEXTO[nivel]}, ${eur(importe)}</title>` : ''}</rect>`;
    x += ancho;
    return rect;
  }).join('');
  const n = d.barras.filter(([, p]) => p >= 2).length;
  cont.innerHTML = `<svg viewBox="0 0 ${huecos} 100" preserveAspectRatio="none" role="img"
    aria-label="Los ${num(d.pagos)} pagos del día ordenados de menos a más riesgo; ${plural(n, 'tiene', 'tienen')} alerta">${barras}</svg>`;
}

// ------------------------------------------------------------------------------ registro de casos
function filaCaso(c, agrupado) {
  const tramo = hora(c.desde) === hora(c.hasta) ? hora(c.desde) : `${hora(c.desde)} a ${hora(c.hasta)}`;
  const donde = c.comercios.length === 1 ? c.comercios[0] : `${c.comercios.length} comercios: ${c.comercios.slice(0, 2).join(', ')}`;
  const [primero, ...resto] = c.motivos;
  const otros = resto.length ? `${resto.slice(0, 2).join('. ')}${resto.length > 2 ? `. Y ${resto.length - 2} más` : ''}` : '';
  const abiertoC = abierto(c.estado);
  return `
    <div class="fila-caso ${c.nivel}${abiertoC ? '' : ' cerrado'}" role="button" tabindex="0"
         data-metodo="${c.metodo_id}" data-dia="${c.dia}" aria-label="Caso ${NIVEL_TEXTO[c.nivel].toLowerCase()}, tarjeta acabada en ${esc(String(c.tarjeta).slice(-4))}">
      <span class="nivel-marca" aria-hidden="true"></span>
      <div class="f-tarjeta"><span class="tarjeta-num">${tarjeta(c.tarjeta)}</span>
        <span class="sub">Cliente ${c.cliente_id}</span>${agrupado ? '' : `<span class="sub">${NIVEL_TEXTO[c.nivel]}</span>`}</div>
      <div class="f-motivos"><span>${esc(primero)}</span>${otros ? `<span class="sub">${esc(otros)}</span>` : ''}${realPill(c.real)}</div>
      <div class="f-cuando"><span>${diaCorto(c.dia)}, ${tramo}</span><span class="sub" title="${esc(c.comercios.join(', '))}">${esc(donde)}</span></div>
      <div class="f-importe"><span class="importe">${eur(c.importe)}</span>${c.transacciones > 1 ? `<span class="sub">${c.transacciones} pagos</span>` : ''}</div>
      <div class="f-accion">${abiertoC ? `<span class="accion ${c.nivel}">${esc(c.accion)}</span>`
        : `<span class="estado ${c.estado}" title="${esc(c.estado_detalle)}">${ESTADO_TEXTO[c.estado]}</span>`}
        <span class="sub">${puesto(c.percentil)}</span></div>
    </div>`;
}

// Cabecera de cada nivel: qué hacer, cuánto suele ser fraude y cuántos quedan por resolver
function cabeceraGrupo(n, r, periodo) {
  const p = r.probabilidad[n].prob;
  const guia = p == null ? '' : p >= 0.1 ? ` De cada 10 casos así, ${sobreDiez(p)} acaban siendo fraude.`
    : ` Solo ${proporcion(p)} casos así acaba siendo fraude.`;
  return `<div class="grupo-cabecera">
      <h2><span class="cuadro ${n}" aria-hidden="true"></span>${NIVEL_GRUPO[n]}</h2>
      <p class="grupo-cifras">${num(r.por_nivel[n])} por resolver de ${num(r.total_por_nivel[n])} ${periodo}</p>
      <p class="grupo-guia">${esc(r.acciones[n])}.${guia}</p>
    </div>`;
}

async function cargarAlertas() {
  const dias = document.getElementById('filtro-dias-alertas').value;
  const [datos, dia] = await Promise.all([
    api('/api/alertas', { comercio: comercio(), estado: estado.filtroEstado, orden: estado.ordenAlertas,
      nivel: document.getElementById('filtro-nivel').value, dias }),
    api('/api/dia', { comercio: comercio() }),
  ]);
  const r = datos.resumen;
  pintarParte(r, dia);
  document.getElementById('nav-pendientes').textContent = (r.por_nivel.critico + r.por_nivel.alto) || '';
  document.getElementById('en-riesgo').innerHTML = r.importe_en_riesgo
    ? `<b>${eur(r.importe_en_riesgo)}</b> en casos por resolver` : '';
  const periodo = dias === '0' ? 'desde el inicio' : `en ${dias} días`;
  const lista = document.getElementById('lista-casos');
  if (!datos.casos.length) {
    lista.innerHTML = estado.filtroEstado === 'pendientes'
      ? '<div class="vacio"><b>Nada por resolver</b><p>Los casos nuevos aparecerán aquí cuando Centinela revise los pagos de esta noche.</p></div>'
      : '<div class="vacio"><b>No hay casos con estos filtros</b><p>Prueba con otro periodo o con otro nivel.</p></div>';
    return;
  }
  // Ordenados por riesgo, los casos van por niveles; en los demás órdenes, en una sola lista
  if (estado.ordenAlertas === 'riesgo') {
    lista.innerHTML = estado.niveles.filter((n) => datos.casos.some((c) => c.nivel === n)).map((n) => `
      <section class="grupo">${cabeceraGrupo(n, r, periodo)}
        ${datos.casos.filter((c) => c.nivel === n).map((c) => filaCaso(c, true)).join('')}</section>`).join('');
  } else {
    lista.innerHTML = `<section class="grupo"><div class="grupo-cabecera"><h2>${datos.casos.length} casos</h2></div>
      ${datos.casos.map((c) => filaCaso(c, false)).join('')}</section>`;
  }
}

// ------------------------------------------------------------------------------ ficha del caso
function tarjetaPago(t, conAcciones) {
  const detalle = [t.categoria, t.canal, t.dispositivo, t.ip_pais && `IP de ${t.ip_pais}`].filter(Boolean).join(', ');
  const motivos = t.motivos.length ? `<ul>${t.motivos.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>` : '';
  const acciones = conAcciones && t.nivel && abierto(t.estado) ? `
    <div class="botones">
      <button class="boton fraude pequeno" data-revisar="fraude" data-ids="${t.transaccion_id}">Es fraude</button>
      <button class="boton legitima pequeno" data-revisar="legitima" data-ids="${t.transaccion_id}">Es legítimo</button>
    </div>` : '';
  return `
    <article class="pago ${t.nivel}">
      <div class="pago-cab">
        <div><b>${hora(t.timestamp)}, ${esc(t.comercio)}</b>
          <p class="sub">${esc(capital(detalle))}${t.estado_pago === 'rechazada' ? '. Pago rechazado' : ''}. Pago ${t.transaccion_id}</p></div>
        <div class="pago-der"><span class="importe">${eur(t.importe)}</span>
          <span class="pills">${nivelPill(t.nivel)}${estadoPill(t.estado, t.estado_detalle)}${realPill(t.real)}</span>
          ${puesto(t.percentil) ? `<span class="sub">${puesto(t.percentil)}</span>` : ''}</div>
      </div>
      ${motivos}
      ${t.comentario ? `<p class="nota-pago">Comentario: ${esc(t.comentario)}</p>` : ''}
      ${t.estado && !abierto(t.estado) ? `<p class="nota-pago">${esc(capital(t.estado_detalle))}.</p>` : ''}
      ${acciones}
    </article>`;
}

// Lo primero que se lee: qué hacer (caso abierto) o cómo se cerró
function bloqueDecision(c) {
  if (!c.nivel) return '';
  if (!abierto(c.estado)) {
    return `<section class="decision-principal ${c.estado}">
      <p class="que">Caso cerrado</p><h3>${RESULTADO[c.estado]}</h3>
      <p>${esc(capital(c.estado_detalle))}.</p></section>`;
  }
  const p = c.probabilidad;
  return `<section class="decision-principal ${c.nivel}">
    <p class="que">Qué hacer</p><h3>${esc(c.accion.corta)}</h3>
    <p>${esc(c.accion.texto)}</p>
    ${p.prob != null ? `<p class="sub">De los casos ${NIVEL_PLURAL[c.nivel]} ya cerrados, ${fraseFraude(p.prob)}
      (${pct(p.prob)} de ${num(p.casos)}).</p>` : ''}</section>`;
}

// La entidad da el veredicto; el comercio solo aporta lo que sabe. Las dos cosas son opcionales
function bloqueRevision(c, ids, n) {
  const cierre = `Si nadie lo revisa, el caso se cierra solo: como fraude si llega el contracargo del banco, o sin
    incidencia si pasan ${c.dias_cierre} días sin reclamación.`;
  if (c.vista === 'comercio') {
    return `<section class="revision">
      <h3>¿Sabes algo de este caso?</h3>
      <p>No tienes que decidir si es fraude: la entidad revisa los casos graves. ${cierre} Si tienes información,
        ayuda a cerrarlo antes y a que Centinela aprenda.</p>
      <textarea id="comentario" aria-label="Comentario" placeholder="Comentario, si quieres: por ejemplo, he llamado al cliente y reconoce la compra"></textarea>
      <div class="botones">
        <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">El cliente reconoce la compra</button>
        <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Confirmo que es fraude</button>
      </div></section>`;
  }
  return `<section class="revision">
    <h3>Veredicto para ${n === 1 ? 'el pago abierto' : `los ${n} pagos abiertos`}</h3>
    <p>Es opcional. ${cierre}</p>
    <textarea id="comentario" aria-label="Comentario" placeholder="Comentario, si quieres: por ejemplo, el titular no reconoce las compras"></textarea>
    <div class="botones">
      <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Es fraude</button>
      <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">${n === 1 ? 'Es legítimo' : 'Son legítimos'}</button>
      <span class="sub">Revisa ${esc(estado.usuario)}. Centinela aprende de cada veredicto.</span>
    </div></section>`;
}

async function abrirCaso(metodo, dia) {
  estado.caso = { metodo, dia, origen: document.activeElement };
  document.getElementById('velo').hidden = false;
  const ficha = document.getElementById('ficha');
  ficha.hidden = false;
  const cuerpo = document.getElementById('ficha-cuerpo');
  cuerpo.innerHTML = '<p class="sub">Cargando el caso…</p>';
  document.getElementById('cerrar-ficha').focus();
  try {
    const c = await api('/api/caso', { metodo_id: metodo, dia, comercio: comercio() });
    const enComercio = c.vista === 'comercio';
    const alertadas = c.transacciones.filter((t) => t.nivel);
    const abiertas = alertadas.filter((t) => abierto(t.estado));
    const total = alertadas.reduce((s, t) => s + t.importe, 0);
    const tj = c.tarjeta || {};
    document.getElementById('ficha-titulo').innerHTML = `
      ${c.nivel ? `<p class="ficha-nivel ${c.nivel}">Caso ${NIVEL_TEXTO[c.nivel].toLowerCase()}</p>` : ''}
      <h2>Tarjeta ${tarjeta(tj.tarjeta)}</h2>
      <p class="sub">${capital(fecha(c.dia).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }))}.
        ${plural(alertadas.length, 'pago', 'pagos')} con alerta por ${eur(total)}.</p>`;
    const titular = enComercio
      ? `<div><dt>Cliente</dt><dd>${tj.cliente_id}<span class="sub">${esc(tj.pais)}</span></dd></div>`
      : `<div><dt>Titular</dt><dd>${esc(tj.cliente)}<span class="sub">Cliente ${tj.cliente_id}, ${esc(tj.pais)}, desde ${tj.alta ? new Date(tj.alta).getFullYear() : '–'}</span></dd></div>`;
    const datosTarjeta = enComercio
      ? `<div><dt>Tarjeta</dt><dd>${esc(tipoTarjeta(tj.tipo))}</dd></div>`
      : `<div><dt>Tarjeta</dt><dd>${esc(tipoTarjeta(tj.tipo))}, ${esc(tj.entidad)}<span class="sub">${tj.limite ? `Límite de ${eur(tj.limite)}` : 'Sin límite de crédito'}</span></dd></div>`;
    cuerpo.innerHTML = `
      ${bloqueDecision(c)}
      <dl class="datos">${titular}${datosTarjeta}
        <div><dt>Pagos con alerta</dt><dd>${alertadas.length}<span class="sub">${abiertas.length ? `${abiertas.length} sin cerrar` : 'Todos cerrados'}</span></dd></div>
      </dl>
      ${c.nivel && abierto(c.estado) && abiertas.length
        ? bloqueRevision(c, abiertas.map((t) => t.transaccion_id).join(','), abiertas.length) : ''}
      <h3>${enComercio ? 'Pagos de esta tarjeta ese día en tu comercio' : 'Pagos de la tarjeta ese día'}</h3>
      ${c.transacciones.map((t) => tarjetaPago(t, !enComercio && abiertas.length > 1)).join('')}
      <h3>${enComercio ? 'Compras anteriores de esta tarjeta en tu comercio' : 'Pagos anteriores de la tarjeta'}</h3>
      ${c.historial.length ? `<div class="tabla-contenedor"><table>
        <thead><tr><th>Fecha</th><th>Comercio</th><th>Canal</th><th class="num">Importe</th><th>Alerta</th></tr></thead>
        <tbody>${c.historial.map((t) => `<tr><td>${fechaHora(t.timestamp)}</td><td>${esc(t.comercio)}</td><td>${esc(t.canal)}</td>
          <td class="num">${eur(t.importe)}</td><td>${nivelPill(t.nivel)} ${estadoPill(t.estado, t.estado_detalle)}</td></tr>`).join('')}</tbody>
      </table></div>` : '<p class="sub">No hay pagos anteriores.</p>'}`;
  } catch (e) {
    mostrarError('ficha-cuerpo', e);
  }
}

function cerrarCaso() {
  if (!estado.caso) return;
  document.getElementById('velo').hidden = true;
  document.getElementById('ficha').hidden = true;
  const origen = estado.caso.origen;
  estado.caso = null;
  if (origen && document.contains(origen)) origen.focus();
}

async function revisar(veredicto, ids) {
  const comentario = document.getElementById('comentario')?.value || '';
  try {
    await api('/api/revisiones', {}, {
      transaccion_ids: ids, veredicto, comentario, usuario: estado.usuario, comercio: Number(comercio()),
    });
    const s = ids.length !== 1 ? 's' : '';
    flotante('Veredicto guardado', `${ids.length} pago${s} marcado${s} como ${veredicto === 'fraude' ? 'fraude' : `legítimo${s}`}. Centinela lo tendrá en cuenta al aprender.`);
    if (estado.caso) await abrirCaso(estado.caso.metodo, estado.caso.dia);
    recargar(['alertas', 'resumen', 'transacciones']);
  } catch (e) {
    flotante('No se ha guardado el veredicto', e.message);
  }
}

// ------------------------------------------------------------------------------ resumen
function cifras(id, lista) {
  document.getElementById(id).innerHTML = lista.map((k) => `
    <div class="cifra">
      <p class="cifra-valor ${k.clase || ''}">${k.valor}</p>
      <p class="cifra-nombre">${esc(k.nombre)}</p>
      <p class="sub">${k.detalle || ''}</p>
    </div>`).join('');
}

async function cargarResumen() {
  const r = await api('/api/resumen', { comercio: comercio(), dias: document.getElementById('filtro-dias-resumen').value });
  document.getElementById('titulo-resumen').textContent =
    `Del ${diaCorto(r.desde)} al ${diaCorto(r.hoy)}${comercio() !== '0' ? `, en ${nombreComercio()}` : ''}`;
  cifras('kpis-resumen', [
    { nombre: 'Casos de alerta', valor: num(r.casos), detalle: `${num(r.abiertos)} por resolver, ${num(r.criticos_abiertos)} críticos` },
    { nombre: 'Por resolver', valor: eur(r.importe_en_riesgo), detalle: 'En casos altos y críticos abiertos' },
    { nombre: 'Fraude confirmado', clase: 'critico', valor: num(r.fraudes_confirmados), detalle: `Pagos, por ${eur(r.importe_fraude)}` },
    { nombre: 'Fraude con alerta, en pagos', clase: 'bien', valor: pct(r.cobertura_pagos),
      detalle: `${num(r.fraudes_con_alerta)} de ${num(r.fraudes_confirmados)} pagos fraudulentos` },
    { nombre: 'Fraude con alerta, en dinero', clase: 'bien', valor: pct(r.cobertura_importe),
      detalle: `${eur(r.importe_con_alerta)} de ${eur(r.importe_fraude)}` },
    { nombre: 'Acierto en casos graves', valor: pct(r.acierto_graves),
      detalle: r.graves_cerrados ? `Eran fraude ${num(Math.round(r.acierto_graves * r.graves_cerrados))} de los ${num(r.graves_cerrados)} casos altos y críticos cerrados` : '' },
  ]);
  const d = r.por_dia;
  grafica('g-dias', {
    data: {
      labels: d.map((x) => diaCorto(x.dia)),
      datasets: [
        ...estado.niveles.map((n) => ({ type: 'bar', label: `Casos ${NIVEL_PLURAL[n]}`, data: d.map((x) => x[n]),
          backgroundColor: COLOR[n], stack: 'casos', order: 2, borderRadius: 1, maxBarThickness: 18 })),
        { type: 'line', label: 'Fraude confirmado', data: d.map((x) => x.fraude), borderColor: COLOR.tinta,
          backgroundColor: COLOR.tinta, borderWidth: 2, tension: 0.25, pointRadius: 0, pointHoverRadius: 4, order: 1 },
      ],
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'top', align: 'start' } },
      scales: { x: { stacked: true, grid: { display: false }, ticks: { maxTicksLimit: 12 } },
        y: { stacked: true, beginAtZero: true, border: { display: false } } },
    },
  });
  const e = r.estados;
  const claves = ['pendiente', 'vigilancia', 'fraude', 'legitima', 'sin_incidencia'].filter((k) => e[k]);
  const textos = { pendiente: 'Por resolver', vigilancia: 'En vigilancia', fraude: 'Fraude', legitima: 'Legítimos', sin_incidencia: 'Sin incidencia' };
  grafica('g-estados', {
    type: 'doughnut',
    data: { labels: claves.map((k) => textos[k]),
      datasets: [{ data: claves.map((k) => e[k]), backgroundColor: claves.map((k) => COLOR[k]), borderWidth: 2, borderColor: '#F5F6F3' }] },
    options: { cutout: '66%', plugins: { legend: { position: 'right' },
      tooltip: { callbacks: { label: (c) => `${c.label}: ${num(c.parsed)} casos` } } } },
  });
  document.getElementById('tarjeta-comercios').hidden = comercio() !== '0';
  if (comercio() === '0') {
    grafica('g-comercios', {
      type: 'bar',
      data: { labels: r.comercios.map((x) => x.comercio), datasets: [
        { label: 'Pagos con alerta', data: r.comercios.map((x) => x.alertas), backgroundColor: '#C9D2CE', maxBarThickness: 14 },
        { label: 'De ellos, fraude', data: r.comercios.map((x) => x.fraude), backgroundColor: COLOR.fraude, maxBarThickness: 14 },
      ] },
      options: { indexAxis: 'y', plugins: { legend: { position: 'top', align: 'start' } },
        scales: { x: { beginAtZero: true, border: { display: false } }, y: { grid: { display: false } } } },
    });
  }
}

// ------------------------------------------------------------------------------ pagos
async function cargarTransacciones() {
  const filas = await api('/api/transacciones', {
    comercio: comercio(), q: document.getElementById('buscar').value,
    solo_alertas: document.getElementById('solo-alertas').checked, orden: estado.ordenTransacciones, limite: 100,
  });
  const cont = document.getElementById('t-transacciones');
  if (!filas.length) {
    cont.innerHTML = '<div class="vacio"><b>No hay pagos que coincidan</b><p>Prueba con el número completo del pago o del cliente, o con los cuatro últimos dígitos de la tarjeta.</p></div>';
    return;
  }
  cont.innerHTML = `<table><thead><tr><th>Fecha</th><th class="num">Pago</th><th>Tarjeta</th><th class="num">Cliente</th><th>Comercio</th>
    <th>Canal</th><th class="num">Importe</th><th>Alerta</th><th>Estado</th>${estado.demo ? '<th>Real</th>' : ''}</tr></thead><tbody>
    ${filas.map((t) => `<tr class="clicable" data-metodo="${t.metodo_id}" data-dia="${String(t.timestamp).slice(0, 10)}" tabindex="0">
      <td>${fechaHora(t.timestamp)}</td><td class="num">${t.transaccion_id}</td><td>${tarjeta(t.tarjeta)}</td>
      <td class="num">${t.cliente_id}</td><td>${esc(t.comercio)}</td><td>${esc(t.canal)}</td>
      <td class="num">${eur(t.importe)}</td><td>${nivelPill(t.nivel)}</td>
      <td>${estadoPill(t.estado, t.estado_detalle)}</td>
      ${estado.demo ? `<td>${realPill(t.real)}</td>` : ''}</tr>`).join('')}
    </tbody></table>`;
}

// ------------------------------------------------------------------------------ avisos
function filaAviso(a) {
  return `<div class="aviso ${a.leido ? '' : 'no-leido'}" role="button" tabindex="0" data-aviso="${a.aviso_id}" data-metodo="${a.metodo_id}" data-dia="${a.dia}">
    <span class="punto" aria-hidden="true"></span>
    <span class="nivel ${a.nivel}">${NIVEL_TEXTO[a.nivel] || ''}</span>
    <div><div class="texto">${esc(a.mensaje)}</div><div class="fecha">${capital(fecha(a.dia).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }))}${a.leido ? '' : '. Sin leer'}</div></div>
    <b class="importe">${eur(a.importe)}</b></div>`;
}

async function cargarAvisos() {
  const datos = await api('/api/avisos', { comercio: comercio(), limite: 200, solo_no_leidos: estado.filtroAvisos === 'no_leidos' });
  actualizarCampana(datos);
  const lista = document.getElementById('lista-avisos');
  lista.innerHTML = datos.avisos.length ? datos.avisos.map(filaAviso).join('')
    : '<div class="vacio"><b>Todo leído</b><p>Cuando Centinela encuentre un caso alto o crítico nuevo, el aviso aparecerá aquí y en la campana.</p></div>';
}

function actualizarCampana(datos) {
  const burbuja = document.getElementById('campana-num');
  burbuja.hidden = !datos.no_leidos;
  burbuja.textContent = datos.no_leidos > 99 ? '99+' : datos.no_leidos;
  document.getElementById('campana').setAttribute('aria-label', datos.no_leidos ? `Avisos: ${datos.no_leidos} sin leer` : 'Avisos');
  document.getElementById('nav-avisos').textContent = datos.no_leidos || '';
}

async function comprobarAvisos(primeraVez = false) {
  try {
    const datos = await api('/api/avisos', { comercio: comercio(), limite: 8 });
    // El pipeline ha puntuado un día nuevo (Airflow, cada noche): se recarga todo lo que se ve
    if (estado.contexto && datos.hoy !== estado.contexto.hoy) {
      await cargarContexto();
      recargar(['alertas', 'resumen', 'transacciones', 'avisos']);
    }
    actualizarCampana(datos);
    document.getElementById('panel-lista').innerHTML = datos.avisos.length ? datos.avisos.map(filaAviso).join('')
      : '<p class="sub" style="padding:12px 14px;margin:0">No hay avisos.</p>';
    // Avisos nuevos desde la última comprobación: aviso flotante
    if (!primeraVez && estado.noLeidos !== null && datos.no_leidos > estado.noLeidos) {
      const nuevo = datos.avisos.find((a) => !a.leido);
      const n = datos.no_leidos - estado.noLeidos;
      flotante(`${plural(n, 'aviso nuevo', 'avisos nuevos')}${datos.criticos_no_leidos ? '. Hay casos críticos' : ''}`,
        nuevo ? nuevo.mensaje : '', nuevo ? () => abrirCaso(nuevo.metodo_id, nuevo.dia) : null);
      recargar(['alertas', 'avisos', 'resumen']);
    }
    estado.noLeidos = datos.no_leidos;
  } catch { /* se reintenta en el siguiente ciclo */ }
}

async function marcarLeidos(cuerpo) {
  await api('/api/avisos/leidos', {}, { ...cuerpo, comercio: Number(comercio()) });
  await comprobarAvisos(true);
  if (estado.pestana === 'avisos') cargarAvisos();
}

// ------------------------------------------------------------------------------ navegación
const CARGADORES = { alertas: cargarAlertas, resumen: cargarResumen, transacciones: cargarTransacciones, avisos: cargarAvisos };
const cargadas = new Set();
const CONTENEDOR_ERROR = { alertas: 'lista-casos', resumen: 'kpis-resumen', transacciones: 't-transacciones', avisos: 'lista-avisos' };

async function cargar(pestana) {
  try {
    await CARGADORES[pestana]();
    cargadas.add(pestana);
  } catch (e) {
    mostrarError(CONTENEDOR_ERROR[pestana], e);
    if (pestana === 'alertas') document.getElementById('parte-fecha').textContent = 'No se ha podido cargar el parte.';
  }
}
function recargar(pestanas) {
  pestanas.forEach((p) => cargadas.delete(p));
  if (pestanas.includes(estado.pestana)) cargar(estado.pestana);
}
function abrir(pestana) {
  estado.pestana = pestana;
  document.querySelectorAll('#pestanas button').forEach((b) => {
    const activa = b.dataset.pestana === pestana;
    b.classList.toggle('activa', activa);
    if (activa) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  document.querySelectorAll('.seccion').forEach((s) => s.classList.toggle('activa', s.id === pestana));
  document.getElementById('parte').hidden = pestana !== 'alertas';   // el parte solo encabeza las alertas
  if (!cargadas.has(pestana)) cargar(pestana);
}

function segmentos(id, alElegir) {
  document.getElementById(id).addEventListener('click', (e) => {
    const b = e.target.closest('button[data-valor]');
    if (!b) return;
    document.querySelectorAll(`#${id} button`).forEach((x) => {
      x.classList.toggle('activa', x === b);
      x.setAttribute('aria-pressed', String(x === b));
    });
    alElegir(b.dataset.valor);
  });
}

// ------------------------------------------------------------------------------ eventos
document.getElementById('pestanas').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-pestana]');
  if (b) abrir(b.dataset.pestana);
});
document.getElementById('comercio').addEventListener('change', (e) => {
  estado.comercio = e.target.value;
  guardar('comercio', estado.comercio);
  estado.noLeidos = null;
  recargar(['alertas', 'resumen', 'transacciones', 'avisos']);
  comprobarAvisos(true);
});
segmentos('filtro-estado', (v) => { estado.filtroEstado = v; cargar('alertas'); });
segmentos('filtro-avisos', (v) => { estado.filtroAvisos = v; cargar('avisos'); });
['filtro-nivel', 'filtro-dias-alertas'].forEach((id) => document.getElementById(id).addEventListener('change', () => cargar('alertas')));
// Orden de las listas (se recuerda en este navegador)
[['orden-alertas', 'ordenAlertas', 'alertas'], ['orden-transacciones', 'ordenTransacciones', 'transacciones']].forEach(([id, clave, pestana]) => {
  const sel = document.getElementById(id);
  sel.value = estado[clave];
  sel.addEventListener('change', () => { estado[clave] = sel.value; guardar(clave, sel.value); cargar(pestana); });
});
document.getElementById('filtro-dias-resumen').addEventListener('change', () => cargar('resumen'));
let temporizador;
document.getElementById('buscar').addEventListener('input', () => { clearTimeout(temporizador); temporizador = setTimeout(() => cargar('transacciones'), 300); });
document.getElementById('solo-alertas').addEventListener('change', () => cargar('transacciones'));
document.getElementById('leer-todos').addEventListener('click', () => marcarLeidos({ todos: true }));
document.getElementById('panel-leer-todos').addEventListener('click', () => marcarLeidos({ todos: true }));

// Abrir un caso desde cualquier sitio: barras del día, registro, pagos y avisos
function activar(e) {
  const revision = e.target.closest('[data-revisar]');
  if (revision) {
    revisar(revision.dataset.revisar, revision.dataset.ids.split(',').map(Number));
    return;
  }
  const ir = e.target.closest('[data-ir]');
  if (ir) { document.getElementById('panel-avisos').hidden = true; abrir(ir.dataset.ir); return; }
  const item = e.target.closest('[data-metodo][data-dia]');
  if (item && !e.target.closest('#ficha')) {
    if (item.dataset.aviso) marcarLeidos({ aviso_ids: [item.dataset.aviso] });
    document.getElementById('panel-avisos').hidden = true;
    abrirCaso(item.dataset.metodo, item.dataset.dia);
  }
}
document.addEventListener('click', activar);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { cerrarCaso(); document.getElementById('panel-avisos').hidden = true; return; }
  // Las filas y las barras se abren también con el teclado
  if ((e.key === 'Enter' || e.key === ' ') && e.target.matches('[data-metodo][data-dia]')) {
    e.preventDefault();
    activar(e);
  }
});
document.getElementById('campana').addEventListener('click', (e) => {
  e.stopPropagation();
  const panel = document.getElementById('panel-avisos');
  panel.hidden = !panel.hidden;
  e.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.campana-zona')) document.getElementById('panel-avisos').hidden = true;
});
document.getElementById('velo').addEventListener('click', cerrarCaso);
document.getElementById('cerrar-ficha').addEventListener('click', cerrarCaso);

const botonUsuario = document.getElementById('cambiar-usuario');
botonUsuario.textContent = estado.usuario;
botonUsuario.addEventListener('click', () => {
  const nombre = (window.prompt('¿Quién revisa los casos?', estado.usuario) || '').trim();
  if (!nombre) return;
  estado.usuario = nombre.slice(0, 80);
  guardar('usuario', estado.usuario);
  botonUsuario.textContent = estado.usuario;
});
const casillaDemo = document.getElementById('modo-demo');
casillaDemo.checked = estado.demo;
casillaDemo.addEventListener('change', () => {
  estado.demo = casillaDemo.checked;
  guardar('demo', estado.demo ? '1' : '0');
  recargar(['alertas', 'resumen', 'transacciones', 'avisos']);
});

// ------------------------------------------------------------------------------ inicio
(async () => {
  try {
    await cargarContexto();
  } catch (e) {
    mostrarError('lista-casos', e);
    document.getElementById('parte-fecha').textContent = 'No se ha podido cargar el parte.';
    return;
  }
  cargar('alertas');
  comprobarAvisos(true);
  setInterval(() => comprobarAvisos(), REFRESCO_MS);
})();
