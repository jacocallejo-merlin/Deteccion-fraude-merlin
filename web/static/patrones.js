'use strict';
// Patrones de fraude (pestaña Resumen, debajo de las cifras): a qué hora, qué día, desde qué dispositivo,
// en qué comercios y qué clientes. Pide /api/patrones y usa las utilidades de app.js (api, grafica,
// num, eur, esc, comercio, COLOR), que se carga antes.

const GRIS_PAGOS = '#C9D2CE';
const DIAS_SEMANA = ['lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom'];
const DISPOSITIVO = { presencial: 'Presencial (datáfono)', movil: 'Móvil', ordenador: 'Ordenador', tablet: 'Tablet' };
const MIN_PAGOS_COMERCIO = 30;      // un comercio con 3 pagos y 1 fraude no es un patrón

// Proporción de fraude con decimales: casi siempre está por debajo del 1 %
const tasa = (f, n) => (n ? f / n : null);
const pctFino = (x) => (x == null ? '–' : `${(x * 100).toLocaleString('es-ES', { maximumFractionDigits: x < 0.1 ? 2 : 1 })} %`);

// Barras con los pagos y línea con el % de fraude en un eje propio
function pagosYTasa(id, etiquetas, filas) {
  grafica(id, {
    data: {
      labels: etiquetas,
      datasets: [
        { type: 'bar', label: 'Pagos', data: filas.map((f) => f.pagos), backgroundColor: GRIS_PAGOS,
          yAxisID: 'y', order: 2, maxBarThickness: 22 },
        { type: 'line', label: '% que es fraude', data: filas.map((f) => tasa(f.fraudes, f.pagos) * 100),
          borderColor: COLOR.fraude, backgroundColor: COLOR.fraude, borderWidth: 2, tension: 0.25,
          pointRadius: 2, yAxisID: 'y1', order: 1 },
      ],
    },
    options: {
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', align: 'start' },
        tooltip: { callbacks: { label: (c) => (c.dataset.yAxisID === 'y1'
          ? `Fraude: ${pctFino(c.parsed.y / 100)} (${num(filas[c.dataIndex].fraudes)} de ${num(filas[c.dataIndex].pagos)})`
          : `Pagos: ${num(c.parsed.y)}`) } },
      },
      scales: {
        x: { grid: { display: false } },
        y: { beginAtZero: true, border: { display: false } },
        y1: { beginAtZero: true, position: 'right', grid: { drawOnChartArea: false }, border: { display: false },
          ticks: { callback: (v) => `${v} %` } },
      },
    },
  });
}

// Barras horizontales con el % de fraude de cada grupo
function barrasTasa(id, etiquetas, filas) {
  grafica(id, {
    type: 'bar',
    data: { labels: etiquetas, datasets: [{ label: '% que es fraude', data: filas.map((f) => tasa(f.fraudes, f.pagos) * 100),
      backgroundColor: COLOR.fraude, maxBarThickness: 14 }] },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: (c) => {
        const f = filas[c.dataIndex];
        return `${pctFino(tasa(f.fraudes, f.pagos))} es fraude (${num(f.fraudes)} de ${num(f.pagos)} pagos)`;
      } } } },
      scales: { x: { beginAtZero: true, border: { display: false }, ticks: { callback: (v) => `${v} %` } },
        y: { grid: { display: false } } },
    },
  });
}

async function cargarPatrones() {
  const caja = document.getElementById('patrones');
  try {
    const p = await api('/api/patrones', { comercio: comercio(), dias: document.getElementById('filtro-dias-resumen').value });
    document.getElementById('patrones-media').textContent = p.pagos
      ? `En el periodo, ${pctFino(p.tasa)} de los pagos son fraude confirmado (${num(p.fraudes)} de ${num(p.pagos)}). `
        + 'Las barras y líneas muestran dónde se concentra.' : 'No hay pagos en el periodo.';

    const horas = Array.from({ length: 24 }, (_, h) => p.por_hora.find((f) => f.hora === h) || { pagos: 0, fraudes: 0 });
    pagosYTasa('g-pat-hora', horas.map((_, h) => `${h} h`), horas);
    const dias = DIAS_SEMANA.map((_, i) => p.por_dia_semana.find((f) => f.dia_semana === i + 1) || { pagos: 0, fraudes: 0 });
    pagosYTasa('g-pat-semana', DIAS_SEMANA, dias);

    const disp = p.por_dispositivo;
    barrasTasa('g-pat-dispositivo',
      disp.map((f) => `${DISPOSITIVO[f.dispositivo] || f.dispositivo}${f.nuevo ? ', nuevo para el cliente' : ''}`), disp);

    document.getElementById('tarjeta-pat-comercios').hidden = comercio() !== '0';
    if (comercio() === '0') {
      const com = p.por_comercio.filter((f) => f.pagos >= MIN_PAGOS_COMERCIO).slice(0, 10);
      barrasTasa('g-pat-comercios', com.map((f) => f.comercio), com);
    }

    document.getElementById('patrones-clientes-sub').textContent = p.clientes_activos
      ? `${num(p.clientes_con_fraude)} de ${num(p.clientes_activos)} clientes con pagos en el periodo han sufrido algún fraude.` : '';
    document.getElementById('t-pat-clientes').innerHTML = p.clientes.length
      ? `<table><thead><tr><th class="num">Cliente</th><th class="num">Pagos</th><th class="num">Fraudes</th>
          <th class="num">Importe defraudado</th></tr></thead><tbody>${p.clientes.map((f) => `
          <tr><td class="num">${esc(f.cliente_id)}</td><td class="num">${num(f.pagos)}</td>
          <td class="num">${num(f.fraudes)}</td><td class="num">${eur(f.importe_fraude)}</td></tr>`).join('')}</tbody></table>`
      : '<p class="sub">Ningún cliente con fraude confirmado en el periodo.</p>';
  } catch (e) {
    // Un fallo aquí no debe tapar el resto del resumen
    caja.querySelector('.patrones-error')?.remove();
    caja.insertAdjacentHTML('afterbegin', `<p class="error patrones-error">${esc(e.message)}</p>`);
  }
}
