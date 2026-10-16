'use strict';
// Página del proyecto antifraude: pide los datos a la API (web/app.py) y dibuja gráficas y tablas.
// Cada pestaña se carga la primera vez que se abre; al cambiar el periodo se vuelve a cargar.

const COLOR = {
  fraude: '#e0524d', legitima: '#5b7db1', alerta: '#f0a03c', anomala: '#8a63d2',
  bien: '#2f9e63', gris: '#a3acbf', acento: '#2f6fde',
};
const NOMBRE_MODELO = {
  iforest: 'Isolation Forest', kmeans: 'KMeans', dbscan: 'DBSCAN', ocsvm: 'One-Class SVM', autoencoder: 'Autoencoder',
};

Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
Chart.defaults.color = '#6b7489';
Chart.defaults.plugins.legend.labels.boxWidth = 12;
Chart.defaults.maintainAspectRatio = false;

const estado = { pestana: 'resumen', cargadas: new Set(), filtroTx: 'todas', modelo: null };
const graficas = {};

// ------------------------------------------------------------------------------ formato
const nf = new Intl.NumberFormat('es-ES');
const num = (n) => (n == null ? '–' : nf.format(n));
const eur = (n) => (n == null ? '–' : n.toLocaleString('es-ES', {
  style: 'currency', currency: 'EUR', maximumFractionDigits: Math.abs(n) >= 1000 ? 0 : 2,
}));
const pct = (x, d = 1) => (x == null || !isFinite(x) ? '–'
  : `${(x * 100).toLocaleString('es-ES', { minimumFractionDigits: d, maximumFractionDigits: d })} %`);
const fecha = (s) => {
  if (!s) return '–';
  const d = new Date(s);
  const dos = (n) => String(n).padStart(2, '0');
  return `${dos(d.getDate())}/${dos(d.getMonth() + 1)}/${String(d.getFullYear()).slice(2)} ${dos(d.getHours())}:${dos(d.getMinutes())}`;
};
const diaCorto = (s) => new Date(`${s}T00:00:00`).toLocaleDateString('es-ES', { day: '2-digit', month: 'short' });
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[c]));
const cociente = (a, b) => (b ? a / b : null);

function pill(texto, clase) { return `<span class="etiqueta-pill pill-${clase}">${esc(texto)}</span>`; }
function etiquetaReal(f) {
  return f.es_fraude ? pill(f.tipo_fraude || 'fraude', 'fraude') : pill('legítima', 'legitima');
}
function barraScore(p) {
  if (p == null) return '–';
  const clase = p >= 0.99 ? 'anomala' : 'gris';
  return `<span class="barrita"><span class="fondo"><span class="relleno" style="width:${(p * 100).toFixed(1)}%;`
    + `background:${clase === 'anomala' ? COLOR.anomala : COLOR.gris}"></span></span>${p.toFixed(3)}</span>`;
}
function veredicto(v) {
  if (!v) return '–';
  return v === 'fraude' ? pill('fraude', 'fraude') : pill('legítima', 'legitima');
}

// ------------------------------------------------------------------------------ API y pintado
function dias() { return document.getElementById('periodo').value; }

async function api(ruta, parametros = {}) {
  const url = new URL(ruta, window.location.origin);
  Object.entries(parametros).forEach(([k, v]) => url.searchParams.set(k, v));
  const respuesta = await fetch(url);
  const cuerpo = await respuesta.json().catch(() => ({}));
  if (!respuesta.ok) throw new Error(cuerpo.detail || `Error ${respuesta.status} en ${ruta}`);
  return cuerpo;
}

function grafica(id, config) {
  if (graficas[id]) graficas[id].destroy();
  graficas[id] = new Chart(document.getElementById(id), config);
}

/** columnas: [{titulo, valor: fila => html, num: bool}] */
function tabla(id, columnas, filas, claseFila = () => '') {
  const contenedor = document.getElementById(id);
  if (!filas || !filas.length) {
    contenedor.innerHTML = '<p class="cargando">Sin datos en este periodo.</p>';
    return;
  }
  const cabecera = columnas.map((c) => `<th class="${c.num ? 'num' : ''}">${esc(c.titulo)}</th>`).join('');
  const cuerpo = filas.map((f) => `<tr class="${claseFila(f)}">${columnas
    .map((c) => `<td class="${c.num ? 'num' : ''}">${c.valor(f)}</td>`).join('')}</tr>`).join('');
  contenedor.innerHTML = `<table><thead><tr>${cabecera}</tr></thead><tbody>${cuerpo}</tbody></table>`;
}
const filaFraude = (f) => (f.es_fraude ? 'es-fraude' : '');

function kpis(id, lista) {
  document.getElementById(id).innerHTML = lista.map((k) => `
    <div class="kpi ${k.clase || ''}">
      <div class="etiqueta">${esc(k.etiqueta)}</div>
      <div class="valor">${k.valor}</div>
      <div class="detalle">${k.detalle || ''}</div>
    </div>`).join('');
}

function mostrarError(seccion, error) {
  const s = document.getElementById(seccion);
  let caja = s.querySelector(':scope > .aviso');
  if (!caja) {
    caja = document.createElement('div');
    caja.className = 'aviso';
    s.prepend(caja);
  }
  caja.textContent = error.message;
}
function quitarError(seccion) {
  document.getElementById(seccion).querySelector(':scope > .aviso')?.remove();
}

// Barras con el porcentaje de fraude de cada valor (tooltip: fraudes de transacciones)
function barrasTasa(id, filas, etiqueta, horizontal = false) {
  grafica(id, {
    type: 'bar',
    data: {
      labels: filas.map(etiqueta),
      datasets: [{ label: '% fraude', data: filas.map((f) => f.tasa * 100), backgroundColor: COLOR.fraude, borderRadius: 4 }],
    },
    options: {
      indexAxis: horizontal ? 'y' : 'x',
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label: (c) => {
              const f = filas[c.dataIndex];
              return `${pct(f.tasa, 2)} fraude (${num(f.fraudes)} de ${num(f.transacciones)})`;
            },
          },
        },
      },
      scales: { [horizontal ? 'x' : 'y']: { ticks: { callback: (v) => `${v} %` } } },
    },
  });
}

// Barras de transacciones + línea con el % de fraude en un segundo eje
function barrasYTasa(id, filas, etiqueta) {
  grafica(id, {
    data: {
      labels: filas.map(etiqueta),
      datasets: [
        { type: 'bar', label: 'Transacciones', data: filas.map((f) => f.transacciones),
          backgroundColor: '#c9d5ea', borderRadius: 3, yAxisID: 'y', order: 2 },
        { type: 'line', label: '% fraude', data: filas.map((f) => (f.fraudes / f.transacciones) * 100),
          borderColor: COLOR.fraude, backgroundColor: COLOR.fraude, tension: 0.3, yAxisID: 'y1', order: 1 },
      ],
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        y: { beginAtZero: true, title: { display: true, text: 'transacciones' } },
        y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false },
          ticks: { callback: (v) => `${v} %` } },
      },
      plugins: { tooltip: { callbacks: { label: (c) => (c.dataset.yAxisID === 'y1'
        ? `% fraude: ${c.parsed.y.toFixed(2)} %` : `Transacciones: ${num(c.parsed.y)}`) } } },
    },
  });
}

// ------------------------------------------------------------------------------ pestañas
async function cargarResumen() {
  const d = dias();
  const [r, serie, patrones, alertas, fraudes] = await Promise.all([
    api('/api/resumen', { dias: d }), api('/api/serie', { dias: d }), api('/api/patrones', { dias: d }),
    api('/api/alertas', { dias: d, limite: 8 }), api('/api/transacciones', { filtro: 'fraude', limite: 8 }),
  ]);
  const t = r.transacciones;
  document.getElementById('rango').textContent = `Datos del ${new Date(t.desde).toLocaleDateString('es-ES')} al `
    + `${new Date(t.hasta).toLocaleDateString('es-ES')} · ${num(t.transacciones)} transacciones · `
    + `${num(t.clientes)} clientes · ${num(t.comercios)} comercios`;

  const lista = [
    { etiqueta: 'Transacciones', valor: num(t.transacciones),
      detalle: `${eur(t.importe)} · ${num(t.rechazadas)} rechazadas` },
    { etiqueta: 'Fraude', clase: 'fraude', valor: num(t.fraudes),
      detalle: `${pct(cociente(t.fraudes, t.transacciones), 2)} de las transacciones · ${eur(t.importe_fraude)}` },
    { etiqueta: 'Confirmado por el banco', clase: 'fraude', valor: pct(cociente(t.confirmados_banco, t.fraudes), 0),
      detalle: `${num(t.confirmados_banco)} de ${num(t.fraudes)} fraudes; el resto aún no (o nunca)` },
  ];
  if (r.reglas) {
    lista.push({ etiqueta: 'Alertas de las reglas', clase: 'alerta', valor: num(r.reglas.transacciones),
      detalle: `transacciones alertadas · ${pct(cociente(r.reglas.fraudes, r.reglas.transacciones))} eran fraude` });
  }
  if (r.analistas) {
    lista.push({ etiqueta: 'Revisadas por analistas', clase: 'bien', valor: num(r.analistas.revisadas),
      detalle: `${pct(cociente(r.analistas.fraude_real, r.analistas.revisadas))} eran fraude` });
  }
  if (r.anomalias) {
    lista.push({ etiqueta: `Anómalas (${NOMBRE_MODELO[r.modelo_base] || r.modelo_base})`, clase: 'anomala',
      valor: num(r.anomalias.anomalas),
      detalle: `más raras que el ${Math.round(r.umbral_anomalia * 100)} % · ${pct(cociente(r.anomalias.fraudes, r.anomalias.anomalas))} eran fraude` });
  }
  kpis('kpis', lista);

  grafica('g-serie', {
    data: {
      labels: serie.map((f) => diaCorto(f.dia)),
      datasets: [
        { type: 'bar', label: 'Transacciones', data: serie.map((f) => f.transacciones), backgroundColor: '#d3dcec', yAxisID: 'y', order: 4 },
        { type: 'line', label: 'Fraudes', data: serie.map((f) => f.fraudes), borderColor: COLOR.fraude, backgroundColor: COLOR.fraude, yAxisID: 'y1', pointRadius: 0, tension: 0.25, borderWidth: 2, order: 1 },
        { type: 'line', label: 'Con alerta de reglas', data: serie.map((f) => f.alertas), borderColor: COLOR.alerta, backgroundColor: COLOR.alerta, yAxisID: 'y1', pointRadius: 0, tension: 0.25, borderWidth: 1.5, order: 2 },
        { type: 'line', label: 'Anómalas', data: serie.map((f) => f.anomalas), borderColor: COLOR.anomala, backgroundColor: COLOR.anomala, yAxisID: 'y1', pointRadius: 0, tension: 0.25, borderWidth: 1.5, order: 3 },
      ],
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { ticks: { maxTicksLimit: 14 } },
        y: { beginAtZero: true, title: { display: true, text: 'transacciones' } },
        y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: 'fraudes / alertas' } },
      },
    },
  });

  grafica('g-donut', {
    type: 'doughnut',
    data: {
      labels: ['Legítimas', 'Fraude'],
      datasets: [{ data: [t.transacciones - t.fraudes, t.fraudes], backgroundColor: [COLOR.legitima, COLOR.fraude], borderWidth: 0 }],
    },
    options: {
      cutout: '62%',
      plugins: { tooltip: { callbacks: { label: (c) => `${c.label}: ${num(c.parsed)} (${pct(c.parsed / t.transacciones, 2)})` } } },
    },
  });

  const tipos = patrones.por_tipo;
  grafica('g-tipos', {
    type: 'bar',
    data: { labels: tipos.map((f) => f.tipo), datasets: [{ label: 'Transacciones fraudulentas', data: tipos.map((f) => f.transacciones), backgroundColor: COLOR.fraude, borderRadius: 4 }] },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { afterLabel: (c) => `Importe: ${eur(tipos[c.dataIndex].importe)}` } } },
    },
  });

  tabla('t-res-alertas', [
    { titulo: 'Fecha', valor: (f) => fecha(f.fecha) },
    { titulo: 'Regla', valor: (f) => pill(f.regla, 'alerta') },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe) },
    { titulo: 'Etiqueta', valor: etiquetaReal },
  ], alertas.ultimas, filaFraude);
  tabla('t-res-fraudes', [
    { titulo: 'Fecha', valor: (f) => fecha(f.timestamp) },
    { titulo: 'Tipo', valor: (f) => pill(f.tipo_fraude, 'fraude') },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe) },
    { titulo: 'Score', valor: (f) => barraScore(f.score_pct) },
    { titulo: 'Reglas', valor: (f) => (f.reglas?.length ? f.reglas.map((x) => pill(x, 'alerta')).join('') : '–') },
  ], fraudes);
}

async function cargarTransacciones() {
  const limite = document.getElementById('limite-tx').value;
  const filas = await api('/api/transacciones', { filtro: estado.filtroTx, limite });
  tabla('t-transacciones', [
    { titulo: 'Fecha', valor: (f) => fecha(f.timestamp) },
    { titulo: 'ID', num: true, valor: (f) => f.transaccion_id },
    { titulo: 'Cliente', num: true, valor: (f) => f.cliente_id },
    { titulo: 'Comercio', valor: (f) => `${esc(f.comercio)}<br><span class="nota">${esc(f.categoria)}</span>` },
    { titulo: 'Canal', valor: (f) => esc(f.canal) },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe) },
    { titulo: 'Estado', valor: (f) => (f.estado === 'rechazada' ? pill('rechazada', 'gris') : esc(f.estado)) },
    { titulo: 'Score', valor: (f) => barraScore(f.score_pct) },
    { titulo: 'Reglas', valor: (f) => (f.reglas?.length ? f.reglas.map((x) => pill(x, 'alerta')).join('') : '–') },
    { titulo: 'Analista', valor: (f) => veredicto(f.veredicto) },
    { titulo: 'Etiqueta', valor: etiquetaReal },
  ], filas, filaFraude);
}

async function cargarPatrones() {
  const p = await api('/api/patrones', { dias: dias() });
  barrasYTasa('g-hora', p.por_hora, (f) => `${f.hora} h`);
  barrasYTasa('g-semana', p.por_dia_semana, (f) => f.nombre);
  grafica('g-senales', {
    type: 'bar',
    data: {
      labels: p.senales.map((f) => f.senal),
      datasets: [{ label: '% fraude', data: p.senales.map((f) => f.tasa * 100),
        backgroundColor: p.senales.map((f) => (f.senal.startsWith('Todas') ? COLOR.gris : COLOR.fraude)), borderRadius: 4 }],
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => {
        const f = p.senales[c.dataIndex];
        return `${pct(f.tasa, 2)} fraude (${num(f.fraudes)} de ${num(f.transacciones)})`;
      } } } },
      scales: { x: { ticks: { callback: (v) => `${v} %` } } },
    },
  });
  barrasTasa('g-canal', p.por_canal, (f) => f.valor);
  barrasTasa('g-dispositivo', p.por_dispositivo, (f) => f.valor);
  barrasTasa('g-categoria', [...p.por_categoria].sort((a, b) => b.tasa - a.tasa), (f) => f.valor, true);
  barrasTasa('g-pais', [...p.por_pais_ip].sort((a, b) => b.tasa - a.tasa), (f) => f.valor, true);
  tabla('t-comercios', [
    { titulo: 'Comercio', valor: (f) => `${esc(f.comercio)}<br><span class="nota">${esc(f.categoria)}</span>` },
    { titulo: 'Transacciones', num: true, valor: (f) => num(f.transacciones) },
    { titulo: 'Fraudes', num: true, valor: (f) => num(f.fraudes) },
    { titulo: '% fraude', num: true, valor: (f) => pct(f.tasa, 2) },
  ], p.top_comercios);
  tabla('t-clientes', [
    { titulo: 'Cliente', valor: (f) => `${esc(f.nombre)}<br><span class="nota">#${f.cliente_id}</span>` },
    { titulo: 'Fraudes', num: true, valor: (f) => `${num(f.fraudes)} / ${num(f.transacciones)}` },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe_fraude) },
    { titulo: 'Tipos', valor: (f) => f.tipos.map((x) => pill(x, 'fraude')).join('') },
  ], p.top_clientes);
}

async function cargarAlertas() {
  const [a, an] = await Promise.all([api('/api/alertas', { dias: dias(), limite: 60 }), api('/api/analistas', { limite: 60 })]);
  const reglas = a.por_regla;
  grafica('g-reglas', {
    type: 'bar',
    data: {
      labels: reglas.map((f) => f.regla),
      datasets: [
        { label: 'Eran fraude', data: reglas.map((f) => f.fraudes), backgroundColor: COLOR.fraude, borderRadius: 3 },
        { label: 'Falsas alarmas', data: reglas.map((f) => f.alertas - f.fraudes), backgroundColor: COLOR.alerta, borderRadius: 3 },
      ],
    },
    options: {
      indexAxis: 'y',
      scales: { x: { stacked: true }, y: { stacked: true } },
      plugins: { tooltip: { callbacks: { footer: (items) => `Precisión: ${pct(reglas[items[0].dataIndex].precision)}` } } },
    },
  });

  if (an.semanal) {
    const s = an.semanal;
    const serie = (clave, texto, color, guiones = []) => ({
      label: texto, data: s.map((f) => (f[clave] == null ? null : f[clave] * 100)), borderColor: color,
      backgroundColor: color, borderDash: guiones, tension: 0.25,
    });
    grafica('g-semanal', {
      type: 'line',
      data: {
        labels: s.map((f) => `Sem. ${f.semana} (${diaCorto(f.desde)})`),
        datasets: [
          serie('precision', 'Cola real (banco + analistas)', COLOR.bien),
          serie('precision_solo_banco', 'Recalibrado solo con el banco', COLOR.acento, [6, 4]),
          serie('precision_kmeans', 'Detector sin recalibrar', COLOR.gris, [2, 3]),
        ],
      },
      options: {
        interaction: { mode: 'index', intersect: false },
        scales: { y: { beginAtZero: true, ticks: { callback: (v) => `${v} %` }, title: { display: true, text: 'alertas que eran fraude' } } },
        plugins: { tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${c.parsed.y?.toFixed(1)} %` } } },
      },
    });
  }

  tabla('t-alertas', [
    { titulo: 'Fecha', valor: (f) => fecha(f.fecha) },
    { titulo: 'Regla', valor: (f) => pill(f.regla, 'alerta') },
    { titulo: 'Riesgo', num: true, valor: (f) => f.nota_riesgo.toFixed(2) },
    { titulo: 'Transacción', num: true, valor: (f) => f.transaccion_id },
    { titulo: 'Cliente', num: true, valor: (f) => f.cliente_id },
    { titulo: 'Comercio', valor: (f) => esc(f.comercio) },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe) },
    { titulo: 'Estado', valor: (f) => pill(f.estado, 'gris') },
    { titulo: 'Etiqueta', valor: etiquetaReal },
  ], a.ultimas, filaFraude);
  tabla('t-por-analista', [
    { titulo: 'Analista', valor: (f) => `${esc(f.analista)}<br><span class="nota">${esc(f.nivel)}</span>` },
    { titulo: 'Revisadas', num: true, valor: (f) => num(f.revisadas) },
    { titulo: 'Marcó fraude', num: true, valor: (f) => num(f.marcadas_fraude) },
    { titulo: 'Acierto', num: true, valor: (f) => pill(pct(f.acierto), f.acierto >= 0.95 ? 'bien' : 'alerta') },
  ], an.por_analista);
  tabla('t-revisiones', [
    { titulo: 'Revisada', valor: (f) => fecha(f.fecha_revision) },
    { titulo: 'Analista', valor: (f) => `${esc(f.analista)}<br><span class="nota">${esc(f.nivel)}</span>` },
    { titulo: 'Veredicto', valor: (f) => veredicto(f.veredicto) },
    { titulo: 'Real', valor: etiquetaReal },
    { titulo: 'Comentario', valor: (f) => `<span class="nota">${esc(f.comentario)}</span>` },
  ], an.ultimas, (f) => ((f.veredicto === 'fraude') !== (f.es_fraude === 1) ? 'es-fraude' : ''));
}

async function cargarAnomalias() {
  const parametros = { dias: dias(), limite: 50 };
  if (estado.modelo) parametros.modelo = estado.modelo;      // la primera vez, el detector elegido
  const a = await api('/api/anomalias', parametros);
  estado.modelo = a.modelo;
  document.getElementById('modelos-anomalia').innerHTML = a.modelos.map((m) => `<button data-modelo="${esc(m)}" `
    + `class="${m === a.modelo ? 'activa' : ''}">${esc(NOMBRE_MODELO[m] || m)}</button>`).join('');

  const r = a.resumen;
  kpis('kpis-anomalia', [
    { etiqueta: `Anómalas (≥ percentil ${Math.round(a.umbral * 100)})`, clase: 'anomala', valor: num(r.anomalas),
      detalle: `${pct(cociente(r.anomalas, r.transacciones), 2)} de las transacciones` },
    { etiqueta: 'De ellas, fraude (precisión)', clase: 'fraude', valor: pct(cociente(r.anomalas_fraude, r.anomalas)),
      detalle: `${num(r.anomalas_fraude)} fraudes · al azar sería ${pct(cociente(r.fraudes, r.transacciones), 2)}` },
    { etiqueta: 'Fraude detectado (recall)', clase: 'bien', valor: pct(cociente(r.anomalas_fraude, r.fraudes)),
      detalle: `${num(r.anomalas_fraude)} de ${num(r.fraudes)} fraudes del periodo` },
  ]);

  const h = a.histograma;
  const tramo = (t) => `${t.toFixed(2)}–${(t + 0.05).toFixed(2)}`;
  grafica('g-histograma', {
    data: {
      labels: h.map((f) => tramo(f.tramo)),
      datasets: [
        { type: 'bar', label: 'Fraudes', data: h.map((f) => f.fraudes), backgroundColor: COLOR.fraude,
          borderRadius: 3, yAxisID: 'y', order: 2 },
        { type: 'line', label: '% de fraude en el tramo', data: h.map((f) => (f.fraudes / f.transacciones) * 100),
          borderColor: COLOR.anomala, backgroundColor: COLOR.anomala, tension: 0.25, yAxisID: 'y1', order: 1 },
      ],
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: { title: { display: true, text: 'percentil del score' } },
        y: { beginAtZero: true, title: { display: true, text: 'fraudes' } },
        y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { callback: (v) => `${v} %` } },
      },
      plugins: { tooltip: { callbacks: { afterBody: (items) => {
        const f = h[items[0].dataIndex];
        return `${num(f.fraudes)} fraudes de ${num(f.transacciones)} transacciones`;
      } } } },
    },
  });

  tabla('t-anomalias', [
    { titulo: 'Fecha', valor: (f) => fecha(f.timestamp) },
    { titulo: 'ID', num: true, valor: (f) => f.transaccion_id },
    { titulo: 'Cliente', num: true, valor: (f) => f.cliente_id },
    { titulo: 'Comercio', valor: (f) => esc(f.comercio) },
    { titulo: 'Canal', valor: (f) => esc(f.canal) },
    { titulo: 'Importe', num: true, valor: (f) => eur(f.importe) },
    { titulo: 'Score', num: true, valor: (f) => f.score.toFixed(3) },
    { titulo: 'Percentil', valor: (f) => barraScore(f.score_pct) },
    { titulo: 'Etiqueta', valor: etiquetaReal },
  ], a.top, filaFraude);
}

async function cargarModelos() {
  const m = await api('/api/modelos');
  if (m.comparativa) {
    const c = m.comparativa;
    const nombre = (f) => `${NOMBRE_MODELO[f.detector] || f.detector}${f.elegido ? ' ★' : ''}`;
    grafica('g-prauc', {
      type: 'bar',
      data: {
        labels: c.map(nombre),
        datasets: [
          { label: 'Validación (elige)', data: c.map((f) => f['PR-AUC_val']), backgroundColor: '#c9d5ea', borderRadius: 3 },
          { label: 'Test (mide)', data: c.map((f) => f['PR-AUC']), backgroundColor: COLOR.acento, borderRadius: 3 },
        ],
      },
      options: { scales: { y: { beginAtZero: true, max: 1 } } },
    });
    grafica('g-presupuesto', {
      type: 'bar',
      data: {
        labels: c.map(nombre),
        datasets: [
          { label: 'Precisión', data: c.map((f) => f['pres_precision_%']), backgroundColor: COLOR.fraude, borderRadius: 3 },
          { label: 'Recall', data: c.map((f) => f['pres_recall_%']), backgroundColor: COLOR.bien, borderRadius: 3 },
        ],
      },
      options: { scales: { y: { beginAtZero: true, ticks: { callback: (v) => `${v} %` } } } },
    });
    tabla('t-comparativa', [
      { titulo: 'Detector', valor: (f) => (f.elegido ? `<b>${esc(nombre(f))}</b>` : esc(nombre(f))) },
      { titulo: 'PR-AUC validación', num: true, valor: (f) => f['PR-AUC_val'].toFixed(3) },
      { titulo: 'PR-AUC test', num: true, valor: (f) => f['PR-AUC'].toFixed(3) },
      { titulo: 'IC 95 %', num: true, valor: (f) => `${f.IC95_inf.toFixed(2)} – ${f.IC95_sup.toFixed(2)}` },
      { titulo: 'ROC-AUC', num: true, valor: (f) => f['ROC-AUC'].toFixed(3) },
      { titulo: 'Lift', num: true, valor: (f) => f.lift.toFixed(1) },
      { titulo: 'Alertas/día (1 %)', num: true, valor: (f) => f.pres_alertas_dia.toFixed(1) },
      { titulo: 'Precisión', num: true, valor: (f) => `${f['pres_precision_%'].toFixed(1)} %` },
      { titulo: 'Recall', num: true, valor: (f) => `${f['pres_recall_%'].toFixed(1)} %` },
    ], c);
  }

  if (m.recall_por_tipo) {
    const filas = m.recall_por_tipo;
    const clave = Object.keys(filas[0])[0];          // primera columna: el tipo de fraude
    const detectores = Object.keys(filas[0]).filter((k) => k !== clave && k !== 'n');
    const celda = (v) => {
      const a = Math.max(0, Math.min(1, v / 100));
      return `<span style="display:inline-block;min-width:52px;padding:2px 6px;border-radius:4px;`
        + `background:rgba(47,158,99,${(0.08 + a * 0.6).toFixed(2)})">${v.toFixed(0)} %</span>`;
    };
    tabla('t-recall-tipo', [
      { titulo: 'Tipo de fraude', valor: (f) => esc(f[clave]) },
      { titulo: 'Casos', num: true, valor: (f) => num(f.n) },
      ...detectores.map((d) => ({ titulo: NOMBRE_MODELO[d] || d, num: true, valor: (f) => celda(f[d]) })),
    ], filas);
  }

  if (m.ajuste_reglas) {
    const filas = m.ajuste_reglas;
    const reglas = [...new Set(filas.map((f) => f.regla))];
    const de = (regla, version) => filas.find((f) => f.regla === regla && f.version === version);
    grafica('g-ajuste', {
      type: 'bar',
      data: {
        labels: reglas,
        datasets: [
          { label: 'Alertas/día con umbrales iniciales', data: reglas.map((r) => de(r, 'inicial').comprobacion_alertas_dia), backgroundColor: COLOR.gris, borderRadius: 3 },
          { label: 'Alertas/día con umbrales ajustados', data: reglas.map((r) => de(r, 'ajustado').comprobacion_alertas_dia), backgroundColor: COLOR.alerta, borderRadius: 3 },
        ],
      },
      options: { scales: { y: { beginAtZero: true, title: { display: true, text: 'alertas al día' } } } },
    });
    tabla('t-ajuste', [
      { titulo: 'Regla', valor: (f) => esc(f.regla) },
      { titulo: 'Umbrales ajustados', valor: (f) => {
        const aj = de(f.regla, 'ajustado');
        return aj.activa ? esc(aj.umbrales) : pill('desactivada', 'gris');
      } },
      { titulo: 'Precisión', num: true, valor: (f) => `${pct(f.comprobacion_precision, 0)} → ${pct(de(f.regla, 'ajustado').comprobacion_precision, 0)}` },
      { titulo: 'Recall de su tipo', num: true, valor: (f) => `${pct(f.comprobacion_recall_tipo, 0)} → ${pct(de(f.regla, 'ajustado').comprobacion_recall_tipo, 0)}` },
    ], filas.filter((f) => f.version === 'inicial'));
  }
  const cfg = m.ajuste_config;
  document.getElementById('ajuste-config').innerHTML = `Recalibrador (desde la fecha de decisión): umbral mínimo de la cola `
    + `<b>${cfg.umbral_minimo ?? 0}</b>; variables añadidas: <b>${esc(cfg.variables_extra.join(', ') || 'ninguna')}</b>; `
    + `variables quitadas: <b>${cfg.variables_quitadas.length}</b>. Detalle en <code>resultados/ajuste_umbrales.md</code>.`;
}

const CARGADORES = {
  resumen: cargarResumen, transacciones: cargarTransacciones, patrones: cargarPatrones,
  alertas: cargarAlertas, anomalias: cargarAnomalias, modelos: cargarModelos,
};

async function cargar(pestana, forzar = false) {
  if (!forzar && estado.cargadas.has(pestana)) return;
  try {
    await CARGADORES[pestana]();
    estado.cargadas.add(pestana);
    quitarError(pestana);
  } catch (e) {
    mostrarError(pestana, e);
    console.error(e);
  }
}

function abrir(pestana) {
  estado.pestana = pestana;
  document.querySelectorAll('#pestanas button').forEach((b) => b.classList.toggle('activa', b.dataset.pestana === pestana));
  document.querySelectorAll('.seccion').forEach((s) => s.classList.toggle('activa', s.id === pestana));
  cargar(pestana);
}

// ------------------------------------------------------------------------------ eventos
document.getElementById('pestanas').addEventListener('click', (e) => {
  if (e.target.dataset.pestana) abrir(e.target.dataset.pestana);
});
document.getElementById('periodo').addEventListener('change', () => {
  estado.cargadas.clear();
  cargar(estado.pestana, true);
  if (estado.pestana !== 'resumen') cargar('resumen', true);   // el rango de la cabecera sale del resumen
});
document.getElementById('actualizar').addEventListener('click', () => {
  estado.cargadas.clear();
  cargar(estado.pestana, true);
});
document.getElementById('filtros-tx').addEventListener('click', (e) => {
  if (!e.target.dataset.filtro) return;
  estado.filtroTx = e.target.dataset.filtro;
  document.querySelectorAll('#filtros-tx button').forEach((b) => b.classList.toggle('activa', b === e.target));
  cargar('transacciones', true);
});
document.getElementById('limite-tx').addEventListener('change', () => cargar('transacciones', true));
document.getElementById('modelos-anomalia').addEventListener('click', (e) => {
  if (!e.target.dataset.modelo) return;
  estado.modelo = e.target.dataset.modelo;
  cargar('anomalias', true);
});

cargar('resumen');
