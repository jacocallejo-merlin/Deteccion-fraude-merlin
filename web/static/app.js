'use strict';
// MERLIN Antifraude: bandeja de alertas, resumen, buscador y avisos. Pide los datos a web/app.py.

const COLOR = { critico: '#d93f3a', alto: '#ee8a1d', medio: '#e6b800', fraude: '#d93f3a', legitima: '#2f9e63',
  pendiente: '#5b7db1', vigilancia: '#b9c6dc', sin_incidencia: '#9aa3b2', acento: '#2f6fde' };
const NIVEL_TEXTO = { critico: 'Crítico', alto: 'Alto', medio: 'Medio' };
const NIVEL_PLURAL = { critico: 'críticos', alto: 'altos', medio: 'medios' };
const ESTADO_TEXTO = { pendiente: 'Por resolver', vigilancia: 'En vigilancia', fraude: 'Fraude', legitima: 'Legítima',
  sin_incidencia: 'Sin incidencia' };
const RESULTADO = { fraude: 'Era fraude', legitima: 'Era una compra legítima', sin_incidencia: 'Cerrado sin incidencia' };
const ABIERTOS = ['pendiente', 'vigilancia'];
const REFRESCO_MS = 60000;

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.color = '#687288';
Chart.defaults.plugins.legend.labels.boxWidth = 12;
Chart.defaults.maintainAspectRatio = false;

const leer = (clave, defecto) => { try { return localStorage.getItem(clave) ?? defecto; } catch { return defecto; } };
const guardar = (clave, valor) => { try { localStorage.setItem(clave, valor); } catch { /* sin almacenamiento */ } };

const estado = {
  pestana: 'alertas', comercio: leer('comercio', '0'), filtroEstado: 'pendientes', filtroAvisos: 'no_leidos',
  usuario: leer('usuario', 'Analista'), demo: leer('demo', '0') === '1', comercios: {},
  noLeidos: null, caso: null,
  ordenAlertas: leer('ordenAlertas', 'riesgo'), ordenTransacciones: leer('ordenTransacciones', 'reciente'),
};
const graficas = {};

// ------------------------------------------------------------------------------ formato
const nf = new Intl.NumberFormat('es-ES');
const num = (n) => (n == null ? '–' : nf.format(n));
const eur = (n) => (n == null ? '–' : n.toLocaleString('es-ES', { style: 'currency', currency: 'EUR' }));
const pct = (x) => (x == null || !isFinite(x) ? '–' : `${Math.round(x * 100)} %`);
const dos = (n) => String(n).padStart(2, '0');
const fechaHora = (s) => { const d = new Date(s); return `${dos(d.getDate())}/${dos(d.getMonth() + 1)} ${dos(d.getHours())}:${dos(d.getMinutes())}`; };
const hora = (s) => { const d = new Date(s); return `${dos(d.getHours())}:${dos(d.getMinutes())}`; };
const fechaLarga = (s) => new Date(`${String(s).slice(0, 10)}T00:00:00`).toLocaleDateString('es-ES', { day: 'numeric', month: 'long', year: 'numeric' });
const diaCorto = (s) => new Date(`${String(s).slice(0, 10)}T00:00:00`).toLocaleDateString('es-ES', { day: '2-digit', month: 'short' });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const tarjeta = (t) => (t ? `•••• ${esc(String(t).slice(-4))}` : 'tarjeta');
const nivelPill = (n) => (n ? `<span class="pill ${n}">${NIVEL_TEXTO[n]}</span>` : '');
const estadoPill = (e, detalle = '') => (e ? `<span class="pill ${e}" title="${esc(detalle)}">${ESTADO_TEXTO[e]}</span>` : '');
const abierto = (e) => ABIERTOS.includes(e);
// Siempre "de cada 10", para que los niveles se comparen de un vistazo: 0,63 -> "6 de cada 10 son
// fraude"; 0,33 -> "3 de cada 10". Solo por debajo del 10 % se pasa a "1 de cada N"
const sobreDiez = (p) => Math.min(Math.round(p * 10), p < 1 ? 9 : 10);
const proporcion = (p) => (p == null ? '' : p >= 0.1 ? `${sobreDiez(p)} de cada 10` : `1 de cada ${Math.round(1 / p)}`);
const fraseFraude = (p) => (p == null ? '' : `${proporcion(p)} ${p >= 0.15 ? 'son' : 'es'} fraude`);
// Puesto del pago entre los del día: 0,0015 -> "top 0,2 % del día" (solo si está entre el 3 % de más riesgo)
const puesto = (x) => (x == null || x > 0.03 ? ''
  : `top ${Math.max(x * 100, 0.1).toLocaleString('es-ES', { maximumFractionDigits: 1 })} % del día`);
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

function grafica(id, config) {
  if (graficas[id]) graficas[id].destroy();
  graficas[id] = new Chart(document.getElementById(id), config);
}

function kpis(id, lista) {
  document.getElementById(id).innerHTML = lista.map((k) => `
    <div class="kpi ${k.clase || ''}">
      <div class="etiqueta">${esc(k.etiqueta)}</div>
      <div class="valor">${k.valor}</div>
      <div class="detalle">${k.detalle || ''}</div>
    </div>`).join('');
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
    `Datos hasta el ${fechaLarga(c.hoy)} · en marcha desde el ${fechaLarga(c.inicio)}`;
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

// ------------------------------------------------------------------------------ alertas
function tarjetaCaso(c) {
  const comercios = c.comercios.length === 1 ? esc(c.comercios[0])
    : `${c.comercios.length} comercios: ${esc(c.comercios.slice(0, 3).join(', '))}${c.comercios.length > 3 ? '…' : ''}`;
  const tramo = hora(c.desde) === hora(c.hasta) ? hora(c.desde) : `${hora(c.desde)}–${hora(c.hasta)}`;
  return `
    <div class="caso ${c.nivel} ${abierto(c.estado) ? '' : 'revisado'}" data-metodo="${c.metodo_id}" data-dia="${c.dia}">
      <div class="franja"></div>
      <div class="cuerpo">
        <div class="linea1">${nivelPill(c.nivel)}
          <span class="titulo">Tarjeta ${tarjeta(c.tarjeta)}</span>
          <span class="nota" style="margin:0">cliente #${c.cliente_id}</span>${realPill(c.real)}</div>
        <div class="linea2">${c.transacciones} pago${c.transacciones > 1 ? 's' : ''} sospechoso${c.transacciones > 1 ? 's' : ''}
          · ${diaCorto(c.dia)} ${tramo} · ${comercios}</div>
        <div>${c.motivos.slice(0, 4).map((m) => `<span class="motivo">${esc(m)}</span>`).join('')}</div>
      </div>
      <div class="lado">
        <span class="importe">${eur(c.importe)}</span>
        ${abierto(c.estado) ? `<span class="accion-corta ${c.nivel}">${esc(c.accion)}</span>` : estadoPill(c.estado, c.estado_detalle)}
        ${puesto(c.percentil) ? `<span class="riesgo">${puesto(c.percentil)}</span>` : ''}
      </div>
    </div>`;
}

async function cargarAlertas() {
  const datos = await api('/api/alertas', {
    comercio: comercio(), estado: estado.filtroEstado, orden: estado.ordenAlertas,
    nivel: document.getElementById('filtro-nivel').value, dias: document.getElementById('filtro-dias-alertas').value,
  });
  const r = datos.resumen;
  const p = r.probabilidad;
  const sel = document.getElementById('filtro-dias-alertas');
  const periodo = sel.value === '0' ? 'desde el inicio' : `· últimos ${sel.value} días`;
  const enPeriodo = sel.value === '0' ? 'desde el inicio' : `en los últimos ${sel.value} días`;
  // Lo que importa: cuántos siguen por resolver y qué hacer; el total del periodo, como contexto
  const kpiNivel = (n) => ({
    etiqueta: `${NIVEL_TEXTO[n]}s por resolver`, clase: n, valor: num(r.por_nivel[n]),
    detalle: `<b>→ ${esc(r.acciones[n])}</b>${p[n].prob != null ? ` · ${fraseFraude(p[n].prob)}` : ''}
      <br>de ${num(r.total_por_nivel[n])} detectados ${enPeriodo}`,
  });
  const cerrados = r.cerrados.fraude + r.cerrados.legitima + r.cerrados.sin_incidencia;
  kpis('kpis-alertas', [
    kpiNivel('critico'),
    kpiNivel('alto'),
    { etiqueta: 'Importe en riesgo', valor: eur(r.importe_en_riesgo), detalle: 'en casos altos y críticos por resolver' },
    r.por_nivel.medio != null
      ? { etiqueta: 'En vigilancia', clase: 'medio', valor: num(r.por_nivel.medio),
        detalle: `casos medios · ${fraseFraude(p.medio.prob) || 'pocos son fraude'} · se cierran solos` }
      : { etiqueta: `Casos cerrados ${periodo}`, clase: 'bien', valor: num(cerrados),
        detalle: `<b>${num(r.cerrados.fraude)} fraude</b> · ${num(r.cerrados.legitima)} legítimos<br>${num(r.cerrados.sin_incidencia)} sin incidencia (${r.dias_cierre} días sin reclamación)` },
  ]);
  document.getElementById('nav-pendientes').textContent = (r.por_nivel.critico + r.por_nivel.alto) || '';
  const lista = document.getElementById('lista-casos');
  if (!datos.casos.length) {
    lista.innerHTML = estado.filtroEstado === 'pendientes'
      ? '<div class="vacio"><b>Nada por resolver</b>No hay casos abiertos con estos filtros.</div>'
      : '<div class="vacio"><b>Sin casos</b>No hay casos con estos filtros.</div>';
    return;
  }
  lista.innerHTML = datos.casos.map(tarjetaCaso).join('');
}

// ------------------------------------------------------------------------------ ficha del caso
function tarjetaPago(t, conAcciones) {
  const motivos = t.motivos.length ? `<ul>${t.motivos.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>` : '';
  const acciones = conAcciones && t.nivel && abierto(t.estado) ? `
    <div class="acciones">
      <button class="boton fraude pequeno" data-revisar="fraude" data-ids="${t.transaccion_id}">Es fraude</button>
      <button class="boton legitima pequeno" data-revisar="legitima" data-ids="${t.transaccion_id}">Es legítima</button>
    </div>` : '';
  return `
    <div class="pago ${t.nivel}">
      <div class="cab">
        <div><b>${hora(t.timestamp)} · ${esc(t.comercio)}</b>
          <div class="nota" style="margin:2px 0 0">${esc(t.categoria)} · ${esc(t.canal)}${t.dispositivo ? ` · ${esc(t.dispositivo)}` : ''}${t.ip_pais ? ` · IP ${esc(t.ip_pais)}` : ''}${t.estado_pago === 'rechazada' ? ' · pago rechazado' : ''} · #${t.transaccion_id}</div></div>
        <div style="text-align:right"><b>${eur(t.importe)}</b><div>${nivelPill(t.nivel)} ${estadoPill(t.estado, t.estado_detalle)} ${realPill(t.real)}</div>
          ${puesto(t.percentil) ? `<div class="riesgo">${puesto(t.percentil)}</div>` : ''}</div>
      </div>
      ${motivos}
      ${t.comentario ? `<div class="nota">Comentario: ${esc(t.comentario)}</div>` : ''}
      ${t.estado && !abierto(t.estado) ? `<div class="nota">${esc(t.estado_detalle)}</div>` : ''}
      ${acciones}
    </div>`;
}

// Qué hacer con el caso (si está abierto) o cómo se cerró
function bloqueRecomendacion(c) {
  if (!c.nivel) return '';
  if (!abierto(c.estado)) {
    return `<div class="recomendacion cerrado ${c.estado}">
      <div class="etiqueta">Caso cerrado</div><b>${RESULTADO[c.estado]}</b>
      <p>${esc(c.estado_detalle.charAt(0).toUpperCase() + c.estado_detalle.slice(1))}.</p></div>`;
  }
  const p = c.probabilidad;
  return `<div class="recomendacion ${c.nivel}">
    <div class="etiqueta">Qué hacer</div><b>${esc(c.accion.corta)}</b>
    <p>${esc(c.accion.texto)}</p>
    ${p.prob != null ? `<p class="nota">Probabilidad de fraude: de los casos ${NIVEL_PLURAL[c.nivel]} ya cerrados,
      ${fraseFraude(p.prob)} (${pct(p.prob)} de ${num(p.casos)}).</p>` : ''}</div>`;
}

// La entidad da el veredicto; el comercio solo aporta lo que sabe (es opcional)
function bloqueDecision(c, ids, n) {
  const cierre = `Si nadie lo revisa, el caso se cierra solo: como fraude si llega el contracargo del banco, o sin
    incidencia si pasan ${c.dias_cierre} días sin reclamación.`;
  if (c.vista === 'comercio') {
    return `<div class="decision">
      <b>¿Sabes algo de este caso? (opcional)</b>
      <p class="nota">No tienes que decidir si es fraude: la entidad revisa los casos graves. ${cierre} Si tienes
        información, ayuda a cerrarlo antes y a que el sistema aprenda.</p>
      <textarea id="comentario" placeholder="Comentario (opcional): p. ej. he llamado al cliente y reconoce la compra"></textarea>
      <div class="botones">
        <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">El cliente reconoce la compra</button>
        <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Confirmo que es fraude</button>
      </div></div>`;
  }
  return `<div class="decision">
    <b>Veredicto para ${n === 1 ? 'el pago abierto' : `los ${n} pagos abiertos`} del caso (opcional)</b>
    <p class="nota">${cierre}</p>
    <textarea id="comentario" placeholder="Comentario (opcional): p. ej. el titular no reconoce las compras"></textarea>
    <div class="botones">
      <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Es fraude</button>
      <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">${n === 1 ? 'Es legítimo' : 'Son legítimos'}</button>
      <span class="nota" style="margin:0">Revisa: <b>${esc(estado.usuario)}</b>. El sistema aprende de este veredicto en la próxima recalibración.</span>
    </div></div>`;
}

async function abrirCaso(metodo, dia) {
  estado.caso = { metodo, dia };
  document.getElementById('velo').hidden = false;
  document.getElementById('ficha').hidden = false;
  const cuerpo = document.getElementById('ficha-cuerpo');
  cuerpo.innerHTML = '<p class="nota">Cargando…</p>';
  try {
    const c = await api('/api/caso', { metodo_id: metodo, dia, comercio: comercio() });
    const enComercio = c.vista === 'comercio';
    const alertadas = c.transacciones.filter((t) => t.nivel);
    const abiertas = alertadas.filter((t) => abierto(t.estado));
    const total = alertadas.reduce((s, t) => s + t.importe, 0);
    const tj = c.tarjeta || {};
    document.getElementById('ficha-titulo').innerHTML = `
      <h2>${nivelPill(c.nivel)} Tarjeta ${tarjeta(tj.tarjeta)}</h2>
      <div class="sub">${fechaLarga(c.dia)} · ${alertadas.length} pago${alertadas.length !== 1 ? 's' : ''} con alerta por ${eur(total)}
        ${c.estado ? ` · ${estadoPill(c.estado, c.estado_detalle)}` : ''}</div>`;
    const titular = enComercio
      ? `<div class="dato"><div class="etiqueta">Cliente</div><div class="valor">#${tj.cliente_id}</div>
          <div class="detalle">${esc(tj.pais)}</div></div>`
      : `<div class="dato"><div class="etiqueta">Titular</div><div class="valor">${esc(tj.cliente)}</div>
          <div class="detalle">#${tj.cliente_id} · ${esc(tj.pais)} · cliente desde ${tj.alta ? new Date(tj.alta).getFullYear() : '–'}</div></div>`;
    const datosTarjeta = enComercio
      ? `<div class="dato"><div class="etiqueta">Tarjeta</div><div class="valor">${esc(tj.tipo)}</div></div>`
      : `<div class="dato"><div class="etiqueta">Tarjeta</div><div class="valor">${esc(tj.tipo)} · ${esc(tj.entidad)}</div>
          <div class="detalle">${tj.limite ? `límite ${eur(tj.limite)}` : 'sin límite de crédito'}</div></div>`;
    cuerpo.innerHTML = `
      ${bloqueRecomendacion(c)}
      <div class="datos">${titular}${datosTarjeta}
        <div class="dato"><div class="etiqueta">Pagos con alerta</div><div class="valor">${alertadas.length}</div>
          <div class="detalle">${abiertas.length ? `${abiertas.length} sin cerrar` : 'todos cerrados'}</div></div>
      </div>
      ${c.nivel && abierto(c.estado) && abiertas.length
        ? bloqueDecision(c, abiertas.map((t) => t.transaccion_id).join(','), abiertas.length) : ''}
      <h3>${enComercio ? 'Pagos de esta tarjeta ese día en tu comercio' : 'Pagos de la tarjeta ese día'}</h3>
      ${c.transacciones.map((t) => tarjetaPago(t, !enComercio && abiertas.length > 1)).join('')}
      <h3>${enComercio ? 'Compras anteriores de esta tarjeta en tu comercio' : 'Historial anterior de la tarjeta'}</h3>
      ${c.historial.length ? `<div class="tarjeta"><div class="tabla-contenedor"><table>
        <thead><tr><th>Fecha</th><th>Comercio</th><th>Canal</th><th class="num">Importe</th><th>Alerta</th></tr></thead>
        <tbody>${c.historial.map((t) => `<tr><td>${fechaHora(t.timestamp)}</td><td>${esc(t.comercio)}</td><td>${esc(t.canal)}</td>
          <td class="num">${eur(t.importe)}</td><td>${nivelPill(t.nivel)} ${estadoPill(t.estado, t.estado_detalle)}</td></tr>`).join('')}</tbody>
      </table></div></div>` : '<p class="nota">Sin pagos anteriores.</p>'}`;
  } catch (e) {
    mostrarError('ficha-cuerpo', e);
  }
}

function cerrarCaso() {
  document.getElementById('velo').hidden = true;
  document.getElementById('ficha').hidden = true;
  estado.caso = null;
}

async function revisar(veredicto, ids) {
  const comentario = document.getElementById('comentario')?.value || '';
  try {
    await api('/api/revisiones', {}, {
      transaccion_ids: ids, veredicto, comentario, usuario: estado.usuario, comercio: Number(comercio()),
    });
    const s = ids.length !== 1 ? 's' : '';
    flotante('Gracias, queda registrado', `${ids.length} pago${s} marcado${s} como ${veredicto === 'fraude' ? 'fraude' : `legítimo${s}`}. El sistema lo tendrá en cuenta al aprender.`);
    if (estado.caso) await abrirCaso(estado.caso.metodo, estado.caso.dia);
    recargar(['alertas', 'resumen', 'transacciones']);
  } catch (e) {
    flotante('No se ha podido guardar', e.message);
  }
}

// ------------------------------------------------------------------------------ resumen
async function cargarResumen() {
  const r = await api('/api/resumen', { comercio: comercio(), dias: document.getElementById('filtro-dias-resumen').value });
  document.getElementById('titulo-resumen').textContent =
    `Resumen del ${diaCorto(r.desde)} al ${diaCorto(r.hoy)}${comercio() !== '0' ? ` · ${estado.comercios[comercio()]}` : ''}`;
  kpis('kpis-resumen', [
    { etiqueta: 'Casos de alerta', valor: num(r.casos), detalle: `<b>${num(r.abiertos)} por resolver</b> · ${num(r.criticos_abiertos)} críticos` },
    { etiqueta: 'Importe en riesgo', clase: 'alto', valor: eur(r.importe_en_riesgo), detalle: 'en casos altos y críticos por resolver' },
    { etiqueta: 'Fraude confirmado', clase: 'critico', valor: num(r.fraudes_confirmados), detalle: `pagos, por ${eur(r.importe_fraude)}` },
    { etiqueta: 'Fraude detectado (pagos)', clase: 'bien', valor: pct(r.cobertura_pagos),
      detalle: `${num(r.fraudes_con_alerta)} de ${num(r.fraudes_confirmados)} pagos fraudulentos tuvieron alerta` },
    { etiqueta: 'Fraude detectado (importe)', clase: 'bien', valor: pct(r.cobertura_importe),
      detalle: `${eur(r.importe_con_alerta)} de ${eur(r.importe_fraude)} defraudados tuvieron alerta` },
    { etiqueta: 'Acierto en casos graves', clase: 'bien', valor: pct(r.acierto_graves),
      detalle: `de ${num(r.graves_cerrados)} casos altos y críticos cerrados eran fraude` },
  ]);
  const d = r.por_dia;
  grafica('g-dias', {
    data: {
      labels: d.map((x) => diaCorto(x.dia)),
      datasets: [
        ...estado.niveles.map((n) => ({ type: 'bar', label: `Casos ${NIVEL_TEXTO[n].toLowerCase()}s`, data: d.map((x) => x[n]), backgroundColor: COLOR[n], stack: 'casos', order: 2 })),
        { type: 'line', label: 'Fraude confirmado', data: d.map((x) => x.fraude), borderColor: COLOR.acento, backgroundColor: COLOR.acento, tension: 0.25, pointRadius: 2, order: 1 },
      ],
    },
    options: { interaction: { mode: 'index', intersect: false }, scales: { x: { stacked: true, ticks: { maxTicksLimit: 15 } }, y: { stacked: true, beginAtZero: true } } },
  });
  const e = r.estados;
  grafica('g-estados', {
    type: 'doughnut',
    data: (() => {   // solo los estados que aparecen (sin nivel medio no hay "en vigilancia")
      const k = ['pendiente', 'vigilancia', 'fraude', 'legitima', 'sin_incidencia'].filter((x) => e[x]);
      const texto = { pendiente: 'Por resolver', vigilancia: 'En vigilancia', fraude: 'Fraude', legitima: 'Legítimos', sin_incidencia: 'Sin incidencia' };
      return { labels: k.map((x) => texto[x]), datasets: [{ data: k.map((x) => e[x]), backgroundColor: k.map((x) => COLOR[x]), borderWidth: 0 }] };
    })(),
    options: { cutout: '62%', plugins: { tooltip: { callbacks: { label: (c) => `${c.label}: ${num(c.parsed)} casos` } } } },
  });
  const tarjetaComercios = document.getElementById('tarjeta-comercios');
  tarjetaComercios.hidden = comercio() !== '0';
  if (comercio() === '0') {
    grafica('g-comercios', {
      type: 'bar',
      data: { labels: r.comercios.map((x) => x.comercio), datasets: [
        { label: 'Pagos con alerta', data: r.comercios.map((x) => x.alertas), backgroundColor: '#c9d5ea' },
        { label: 'De ellos, fraude', data: r.comercios.map((x) => x.fraude), backgroundColor: COLOR.fraude },
      ] },
      options: { indexAxis: 'y', scales: { x: { beginAtZero: true } } },
    });
  }
}

// ------------------------------------------------------------------------------ transacciones
async function cargarTransacciones() {
  const filas = await api('/api/transacciones', {
    comercio: comercio(), q: document.getElementById('buscar').value,
    solo_alertas: document.getElementById('solo-alertas').checked, orden: estado.ordenTransacciones, limite: 100,
  });
  const cont = document.getElementById('t-transacciones');
  if (!filas.length) { cont.innerHTML = '<p class="nota">No hay transacciones que coincidan.</p>'; return; }
  cont.innerHTML = `<table><thead><tr><th>Fecha</th><th>ID</th><th>Tarjeta</th><th>Cliente</th><th>Comercio</th>
    <th>Canal</th><th class="num">Importe</th><th>Alerta</th><th>Estado</th>${estado.demo ? '<th>Real</th>' : ''}</tr></thead><tbody>
    ${filas.map((t) => `<tr class="clicable" data-metodo="${t.metodo_id}" data-dia="${String(t.timestamp).slice(0, 10)}">
      <td>${fechaHora(t.timestamp)}</td><td class="num">${t.transaccion_id}</td><td>${tarjeta(t.tarjeta)}</td>
      <td class="num">${t.cliente_id}</td><td>${esc(t.comercio)}</td><td>${esc(t.canal)}</td>
      <td class="num">${eur(t.importe)}</td><td>${nivelPill(t.nivel)}${puesto(t.percentil) ? ` <span class="riesgo">${puesto(t.percentil)}</span>` : ''}</td>
      <td>${estadoPill(t.estado, t.estado_detalle)}</td>
      ${estado.demo ? `<td>${realPill(t.real)}</td>` : ''}</tr>`).join('')}
    </tbody></table>`;
}

// ------------------------------------------------------------------------------ avisos
function filaAviso(a) {
  return `<div class="aviso ${a.leido ? '' : 'no-leido'}" data-aviso="${a.aviso_id}" data-metodo="${a.metodo_id}" data-dia="${a.dia}">
    ${nivelPill(a.nivel)}
    <div><div class="texto">${esc(a.mensaje)}</div><div class="fecha">${fechaLarga(a.dia)}</div></div>
    <b>${eur(a.importe)}</b></div>`;
}

async function cargarAvisos() {
  const datos = await api('/api/avisos', { comercio: comercio(), limite: 200, solo_no_leidos: estado.filtroAvisos === 'no_leidos' });
  actualizarCampana(datos);
  const lista = document.getElementById('lista-avisos');
  lista.innerHTML = datos.avisos.length ? datos.avisos.map(filaAviso).join('')
    : '<div class="vacio"><b>Nada nuevo</b>No hay avisos sin leer.</div>';
}

function actualizarCampana(datos) {
  const burbuja = document.getElementById('campana-num');
  burbuja.hidden = !datos.no_leidos;
  burbuja.textContent = datos.no_leidos > 99 ? '99+' : datos.no_leidos;
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
      : '<p class="nota" style="padding:12px 14px">No hay avisos.</p>';
    // Avisos nuevos desde la última comprobación: aviso flotante
    if (!primeraVez && estado.noLeidos !== null && datos.no_leidos > estado.noLeidos) {
      const nuevo = datos.avisos.find((a) => !a.leido);
      const n = datos.no_leidos - estado.noLeidos;
      flotante(`${n} aviso${n > 1 ? 's' : ''} nuevo${n > 1 ? 's' : ''}${datos.criticos_no_leidos ? ' · hay casos críticos' : ''}`,
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
  }
}
function recargar(pestanas) {
  pestanas.forEach((p) => cargadas.delete(p));
  if (pestanas.includes(estado.pestana)) cargar(estado.pestana);
}
function abrir(pestana) {
  estado.pestana = pestana;
  document.querySelectorAll('#pestanas button').forEach((b) => b.classList.toggle('activa', b.dataset.pestana === pestana));
  document.querySelectorAll('.seccion').forEach((s) => s.classList.toggle('activa', s.id === pestana));
  if (!cargadas.has(pestana)) cargar(pestana);
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
document.getElementById('filtro-estado').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-valor]');
  if (!b) return;
  estado.filtroEstado = b.dataset.valor;
  document.querySelectorAll('#filtro-estado button').forEach((x) => x.classList.toggle('activa', x === b));
  cargar('alertas');
});
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
document.getElementById('filtro-avisos').addEventListener('click', (e) => {
  const b = e.target.closest('button[data-valor]');
  if (!b) return;
  estado.filtroAvisos = b.dataset.valor;
  document.querySelectorAll('#filtro-avisos button').forEach((x) => x.classList.toggle('activa', x === b));
  cargar('avisos');
});
document.getElementById('leer-todos').addEventListener('click', () => marcarLeidos({ todos: true }));
document.getElementById('panel-leer-todos').addEventListener('click', () => marcarLeidos({ todos: true }));

// Abrir un caso desde cualquier lista (casos, transacciones, avisos)
document.addEventListener('click', (e) => {
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
});
document.getElementById('campana').addEventListener('click', (e) => {
  e.stopPropagation();
  const panel = document.getElementById('panel-avisos');
  panel.hidden = !panel.hidden;
});
document.addEventListener('click', (e) => {
  if (!e.target.closest('.campana-zona')) document.getElementById('panel-avisos').hidden = true;
});
document.getElementById('velo').addEventListener('click', cerrarCaso);
document.getElementById('cerrar-ficha').addEventListener('click', cerrarCaso);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') cerrarCaso(); });

const botonUsuario = document.getElementById('cambiar-usuario');
botonUsuario.textContent = estado.usuario;
botonUsuario.addEventListener('click', () => {
  const nombre = (window.prompt('¿Quién revisa las alertas?', estado.usuario) || '').trim();
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
    return;
  }
  cargar('alertas');
  comprobarAvisos(true);
  setInterval(() => comprobarAvisos(), REFRESCO_MS);
})();
