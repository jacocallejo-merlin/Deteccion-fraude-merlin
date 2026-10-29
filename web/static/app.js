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
  usuario: '', demo: leer('demo', '0') === '1', comercios: {},
  noLeidos: null, caso: null, niveles: ['critico', 'alto'], alertas: null,
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
  // Sin sesión (o caducada): a iniciar sesión, y de vuelta aquí al entrar
  if (r.status === 401 && ruta !== '/api/login') location.href = `/login?volver=${encodeURIComponent(location.href)}`;
  const datos = await r.json().catch(() => ({}));
  if (r.status === 403 && datos.cambiar_contrasena) location.href = '/login?cambiar=1';   // contraseña provisional
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
  // La web de los modelos: la que diga el servidor (despliegue con una sola dirección) o, si no, la misma
  // máquina en el puerto 8001 (local, Docker y port-forward de k8s)
  document.getElementById('ir-modelos').href = c.web_modelos || `${location.protocol}//${location.hostname}:8001/`;
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
// Arriba, solo lo esencial: lo revisado esta noche, cuántos casos de cada nivel esperan una decisión y por
// dónde empezar. Las cifras de cada nivel llevan a su recuadro
function pintarParte(r, d, casos) {
  const quien = comercio() === '0' ? '' : ` de ${nombreComercio()}`;
  const graves = estado.niveles.filter((n) => n !== 'medio');
  const pendientes = graves.reduce((s, n) => s + (r.por_nivel[n] || 0), 0);
  document.getElementById('parte-fecha').textContent =
    capital(fecha(d.dia).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }));
  document.getElementById('parte-titulo').textContent = pendientes
    ? `${plural(pendientes, 'caso necesita', 'casos necesitan')} tu decisión`
    : 'Todo en orden: nada por resolver';
  document.getElementById('parte-guia').textContent = pendientes
    ? `Empieza por los ${r.por_nivel.critico ? 'críticos' : 'altos'}: pulsa un caso para ver qué ha pasado y decidir si es fraude.`
    : 'Centinela ha revisado los pagos y no hay nada sospechoso pendiente. Los casos nuevos aparecerán aquí.';
  // Los casos nuevos de esta noche, de cada nivel (se saben con la lista de los que están por resolver)
  const nuevos = (n) => (casos ? casos.filter((c) => c.nivel === n && c.dia === d.dia).length : 0);
  const cifra = (valor, texto, extra = '', clase = '', destino = '') => {
    const etiqueta = destino ? 'a' : 'div';
    return `<${etiqueta} class="parte-cifra ${clase}"${destino ? ` href="#${destino}"` : ''}>
      <span class="parte-num">${valor}</span><span class="parte-que">${texto}</span>${extra ? `<span class="parte-extra">${extra}</span>` : ''}</${etiqueta}>`;
  };
  document.getElementById('parte-cifras').innerHTML = [
    cifra(num(d.pagos), `pagos${quien} revisados esta noche`),
    ...graves.map((n) => cifra(num(r.por_nivel[n] || 0), `${NIVEL_PLURAL[n]} por resolver`,
      nuevos(n) ? plural(nuevos(n), 'nuevo hoy', 'nuevos hoy') : '', n, casos ? `caja-${n}` : '')),
    cifra(eur(r.importe_en_riesgo), 'en juego en los casos por resolver', '', 'dinero'),
  ].join('');
}

// ------------------------------------------------------------------------------ casos por resolver
// Un recuadro por nivel, del más grave al menos, con qué hacer y cada caso como una tarjeta que se pulsa.
// Salen todos los casos, sin recortar: todos piden una decisión
const PRIORIDAD = { critico: 3, alto: 2, medio: 1 };

function tarjetaCaso(c) {
  const donde = c.comercios.length === 1 ? c.comercios[0] : `${c.comercios.length} comercios`;
  const resto = c.motivos.length - 1;
  return `<div class="tarjeta-caso ${c.nivel}" role="button" tabindex="0" data-metodo="${c.metodo_id}" data-dia="${c.dia}"
      aria-label="Caso ${NIVEL_TEXTO[c.nivel].toLowerCase()}, tarjeta acabada en ${esc(String(c.tarjeta).slice(-4))}, ${eur(c.importe)}. Abrir el caso">
    <div class="tc-cabeza"><span class="tarjeta-num">${tarjeta(c.tarjeta)}</span><span class="importe">${eur(c.importe)}</span></div>
    <p class="tc-motivo">${esc(c.motivos[0])}${resto > 0 ? `<span class="sub"> y ${plural(resto, 'motivo más', 'motivos más')}</span>` : ''}</p>
    <p class="sub tc-donde">${plural(c.transacciones, 'pago', 'pagos')} en ${esc(donde)}</p>
    <div class="tc-pie"><span class="sub">${diaCorto(c.dia)}, ${hora(c.desde)}</span>${realPill(c.real)}<span class="tc-abrir">Revisar</span></div>
  </div>`;
}

function cajaNivel(n, casos, r) {
  const p = r.probabilidad[n].prob;
  const guia = p == null ? '' : p >= 0.1 ? ` De cada 10 casos así, ${sobreDiez(p)} acaban siendo fraude.`
    : ` Solo ${proporcion(p)} casos así acaba siendo fraude.`;
  const total = casos.reduce((s, c) => s + c.importe, 0);
  return `<section class="caja ${n}" id="caja-${n}" aria-labelledby="caja-titulo-${n}">
    <div class="caja-cabecera">
      <h2 id="caja-titulo-${n}"><span class="cuadro ${n}" aria-hidden="true"></span>${NIVEL_GRUPO[n]}
        <span class="caja-num">${casos.length ? `${num(casos.length)} por resolver` : 'ninguno por resolver'}</span>
        ${casos.length ? `<span class="caja-total">${eur(total)} en total</span>` : ''}</h2>
      ${casos.length ? `<p class="caja-guia"><b>Qué hacer: ${esc(r.acciones[n].toLowerCase())}.</b>${guia}</p>` : ''}
    </div>
    ${casos.length ? `<div class="tarjetas-casos">${casos.map(tarjetaCaso).join('')}</div>`
      : `<p class="caja-vacia">No hay casos ${NIVEL_PLURAL[n]} pendientes.</p>`}
  </section>`;
}

function pintarCajas() {
  const { casos, resumen } = estado.alertas;
  const minimo = Number(document.getElementById('filtro-nivel').value);
  document.getElementById('lista-casos').innerHTML = estado.niveles.filter((n) => PRIORIDAD[n] >= minimo)
    .map((n) => cajaNivel(n, casos.filter((c) => c.nivel === n), resumen)).join('');
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
  const porResolver = estado.filtroEstado === 'pendientes';
  pintarParte(r, dia, porResolver ? datos.casos : null);
  document.getElementById('nav-pendientes').textContent = (r.por_nivel.critico + r.por_nivel.alto) || '';
  // Por resolver: un recuadro por nivel. Cerrados o todos: el registro de siempre, en filas
  if (porResolver) {
    estado.alertas = datos;
    pintarCajas();
    return;
  }
  const periodo = dias === '0' ? 'desde el inicio' : `en ${dias} días`;
  const lista = document.getElementById('lista-casos');
  if (!datos.casos.length) {
    lista.innerHTML = '<div class="vacio"><b>No hay casos con estos filtros</b><p>Prueba con otro periodo o con otro nivel.</p></div>';
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
// Pensada para quien no conoce Centinela: en orden, qué hacer, por qué es sospechoso, los pagos, quién es el
// titular y cómo suele pagar. La pregunta "¿es fraude?" va fija abajo, siempre a la vista

// Las reglas, explicadas en una frase (las que no estén aquí salen solo con su nombre)
const REGLAS = {
  'Pico de gasto anómalo': 'Gasta mucho más de lo habitual en poco tiempo.',
  'Card testing': 'Varios pagos pequeños seguidos: es lo típico de quien prueba si una tarjeta robada funciona.',
  'Cash-out multicomercio': 'Compras en varios comercios en poco tiempo, para sacar todo lo posible antes de que se bloquee la tarjeta.',
  'Dispositivo nuevo de alto riesgo': 'Paga desde un dispositivo que el cliente no había usado nunca, junto con otras señales de riesgo.',
  'IP/geolocalización sospechosa': 'La conexión viene de un sitio que no encaja con el cliente.',
  'Account takeover': 'Alguien podría haber entrado en la cuenta del cliente: cambios de datos, intentos de acceso o un dispositivo nuevo.',
  'Bust-out': 'La tarjeta se usaba con normalidad y de repente agota el crédito: típico de quien no piensa pagar.',
  'Actividad tipo bot': 'Acciones demasiado rápidas para una persona: probablemente un programa automático.',
  'Estructuración (smurfing)': 'Un importe grande repartido en varios pagos pequeños para no llamar la atención.',
};
const CANAL_TEXTO = { app: 'por la app', web: 'por la web', datafono: 'en tienda, con datáfono' };
const DISPOSITIVO_TEXTO = { movil: 'desde el móvil', ordenador: 'desde un ordenador', tablet: 'desde una tableta' };

function listaMotivos(motivos) {
  return `<ul class="motivos">${motivos.map((m) => {
    if (!m.startsWith('Regla: ')) return `<li>${esc(m)}</li>`;
    const regla = m.slice(7);
    return `<li><b>${esc(regla)}</b>${REGLAS[regla] ? `<span class="sub">${esc(REGLAS[regla])}</span>` : ''}</li>`;
  }).join('')}</ul>`;
}

// Un pago, en limpio: dónde, cuánto, cuándo y cómo. Si ya está cerrado, cómo acabó
function tarjetaPago(t) {
  const como = [CANAL_TEXTO[t.canal] || t.canal, DISPOSITIVO_TEXTO[t.dispositivo], t.ip_pais && `con conexión desde ${t.ip_pais}`]
    .filter(Boolean).join(', ');
  const cerrado = t.estado && !abierto(t.estado);
  return `
    <article class="pago ${t.nivel}">
      <div class="pago-cab">
        <div><b>${esc(t.comercio)}</b>${t.categoria ? `<span class="sub"> ${esc(capital(t.categoria))}</span>` : ''}</div>
        <span class="importe">${eur(t.importe)}</span>
      </div>
      <p class="sub">A las ${hora(t.timestamp)}, ${esc(como)}.${t.estado_pago === 'rechazada' ? ' El pago fue rechazado.' : ''}
        Pago nº ${t.transaccion_id}. ${realPill(t.real)}</p>
      ${cerrado ? `<p class="nota-pago ${t.estado}">${RESULTADO[t.estado]}: ${esc(t.estado_detalle)}.</p>` : ''}
      ${t.comentario ? `<p class="nota-pago">Comentario: ${esc(t.comentario)}</p>` : ''}
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
  const p = c.probabilidad.prob;
  const guia = p == null ? '' : p >= 0.1 ? `De cada 10 casos como este, ${sobreDiez(p)} acaban siendo fraude.`
    : `Solo ${proporcion(p)} casos como este acaba siendo fraude.`;
  return `<section class="decision-principal ${c.nivel}">
    <p class="que">Qué hacer</p><h3>${esc(c.accion.corta)}</h3>
    <p>${esc(c.accion.texto)}</p>
    ${guia ? `<p class="probabilidad">${guia}</p>` : ''}</section>`;
}

// Cómo suele pagar la tarjeta: una frase con lo normal y, plegada, la lista de sus últimos pagos
function bloqueHistorial(c, enComercio) {
  const h = c.historial;
  const donde = enComercio ? ' en tu comercio' : '';
  if (!h.length) return `<p>Es la primera vez que vemos pagos de esta tarjeta${donde}: no hay nada con qué comparar.</p>`;
  const importes = h.map((t) => t.importe).sort((a, b) => a - b);
  const mediana = importes[Math.floor(importes.length / 2)];
  const conAlerta = h.filter((t) => t.nivel).length;
  const fraudes = h.filter((t) => t.estado === 'fraude').length;
  const cuantos = h.length >= 15 ? 'sus últimos 15 pagos' : plural(h.length, 'pago anterior', 'pagos anteriores');
  const alertas = h.length === 1 ? (fraudes ? 'fue fraude' : conAlerta ? 'tuvo alerta' : 'no tuvo alerta')
    : conAlerta ? `${plural(conAlerta, 'tuvo alerta', 'tuvieron alerta')}${fraudes ? ` y ${plural(fraudes, 'fue fraude', 'fueron fraude')}` : ''}`
      : 'ninguno tuvo alerta';
  const frase = h.length === 1 ? `Solo tiene un pago anterior${donde}, de ${eur(mediana)}, y ${alertas}.`
    : `Normalmente paga unos <b>${eur(mediana)}</b>${donde}. De ${cuantos}, ${alertas}.`;
  return `<p>${frase}</p>
    <details class="desplegable"><summary>Ver ${cuantos}</summary>
      <table class="historial"><tbody>${h.map((t) => `<tr><td>${fechaHora(t.timestamp)}</td><td>${esc(t.comercio)}</td>
        <td class="num">${eur(t.importe)}</td><td>${t.nivel ? nivelPill(t.nivel) : ''} ${t.estado === 'fraude' ? estadoPill('fraude') : ''}</td></tr>`).join('')}
      </tbody></table></details>`;
}

// La pregunta, fija abajo. La entidad da el veredicto; el comercio solo cuenta lo que sabe. Las dos cosas son opcionales
function pieRevision(c, abiertas, enComercio) {
  const pie = document.getElementById('ficha-pie');
  pie.hidden = !(c.nivel && abierto(c.estado) && abiertas.length);
  if (pie.hidden) { pie.innerHTML = ''; return; }
  if (estado.sesion?.invitado) {
    pie.innerHTML = '<p class="sub">Estás como invitado: puedes ver el caso, pero no responder.</p>';
    return;
  }
  const ids = abiertas.map((t) => t.transaccion_id).join(',');
  const cierre = `Si nadie responde, se cierra solo: como fraude si llega un contracargo, o sin incidencia a los ${c.dias_cierre} días.`;
  pie.innerHTML = enComercio ? `
      <p class="pie-pregunta"><b>¿Sabes algo de este caso?</b>
        <span class="sub">No tienes que decidir tú: la entidad revisa los casos graves. ${cierre}</span></p>
      <details class="desplegable"><summary>Añadir un comentario</summary>
        <textarea id="comentario" aria-label="Comentario" placeholder="Por ejemplo: he llamado al cliente y reconoce la compra"></textarea></details>
      <div class="botones">
        <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">El cliente reconoce la compra</button>
        <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Confirmo que es fraude</button>
      </div>` : `
      <p class="pie-pregunta"><b>¿Es fraude?</b>
        <span class="sub">Tu respuesta cierra el caso y Centinela aprende de ella. ${cierre}</span></p>
      <details class="desplegable"><summary>Añadir un comentario</summary>
        <textarea id="comentario" aria-label="Comentario" placeholder="Por ejemplo: el titular no reconoce las compras"></textarea></details>
      <div class="botones">
        <button class="boton fraude" data-revisar="fraude" data-ids="${ids}">Sí, es fraude</button>
        <button class="boton legitima" data-revisar="legitima" data-ids="${ids}">No, es una compra legítima</button>
      </div>`;
}

async function abrirCaso(metodo, dia) {
  estado.caso = { metodo, dia, origen: document.activeElement };
  document.getElementById('velo').hidden = false;
  const ficha = document.getElementById('ficha');
  ficha.hidden = false;
  const cuerpo = document.getElementById('ficha-cuerpo');
  cuerpo.innerHTML = '<p class="sub">Cargando el caso…</p>';
  document.getElementById('ficha-pie').hidden = true;
  document.getElementById('cerrar-ficha').focus();
  try {
    const c = await api('/api/caso', { metodo_id: metodo, dia, comercio: comercio() });
    const enComercio = c.vista === 'comercio';
    const alertadas = c.transacciones.filter((t) => t.nivel);
    const otros = c.transacciones.filter((t) => !t.nivel);
    const abiertas = alertadas.filter((t) => abierto(t.estado));
    const total = alertadas.reduce((s, t) => s + t.importe, 0);
    const motivos = [...new Set(alertadas.flatMap((t) => t.motivos))];
    const tj = c.tarjeta || {};
    document.getElementById('ficha-titulo').innerHTML = `
      ${c.nivel ? `<p class="ficha-nivel ${c.nivel}">Caso ${NIVEL_TEXTO[c.nivel].toLowerCase()}</p>` : ''}
      <h2>Tarjeta ${tarjeta(tj.tarjeta)}</h2>
      <p class="sub">${capital(fecha(c.dia).toLocaleDateString('es-ES', { weekday: 'long', day: 'numeric', month: 'long' }))}.
        ${alertadas.length ? `${capital(plural(alertadas.length, 'pago sospechoso', 'pagos sospechosos'))} por ${eur(total)}.` : 'Ningún pago sospechoso.'}</p>`;
    const titular = enComercio
      ? `<dt>Cliente</dt><dd>Nº ${tj.cliente_id}, ${esc(tj.pais)}</dd><dt>Tarjeta</dt><dd>${esc(tipoTarjeta(tj.tipo))}</dd>`
      : `<dt>Titular</dt><dd>${esc(tj.cliente)}</dd>
         <dt>Cliente</dt><dd>Nº ${tj.cliente_id}, ${esc(tj.pais)}${tj.alta ? `, desde ${new Date(tj.alta).getFullYear()}` : ''}</dd>
         <dt>Tarjeta</dt><dd>${esc(tipoTarjeta(tj.tipo))} de ${esc(tj.entidad)}, ${tj.limite ? `límite de ${eur(tj.limite)}` : 'sin límite de crédito'}</dd>`;
    cuerpo.innerHTML = `
      ${bloqueDecision(c)}
      ${motivos.length ? `<section class="ficha-bloque"><h3>Por qué es sospechoso</h3>${listaMotivos(motivos)}</section>` : ''}
      <section class="ficha-bloque">
        <h3>${alertadas.length === 1 ? 'El pago sospechoso' : alertadas.length ? `Los ${alertadas.length} pagos sospechosos` : 'Pagos de la tarjeta ese día'}</h3>
        ${(alertadas.length ? alertadas : otros).map(tarjetaPago).join('')}
        ${alertadas.length && otros.length ? `<details class="desplegable"><summary>Ver ${plural(otros.length, 'pago más', 'pagos más')} de la tarjeta ese día, sin alerta</summary>
          ${otros.map(tarjetaPago).join('')}</details>` : ''}
      </section>
      <section class="ficha-bloque"><h3>${enComercio ? 'El cliente' : 'El titular'}</h3><dl class="titular">${titular}</dl></section>
      <section class="ficha-bloque"><h3>Cómo suele pagar</h3>${bloqueHistorial(c, enComercio)}</section>`;
    pieRevision(c, abiertas, enComercio);
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
  await cargarPatrones();      // patrones.js: hora, dispositivo, comercios y clientes con más fraude
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
      document.querySelector('.bienvenida')?.remove();   // un comercio recién registrado: ya le han llegado pagos
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

// Quién ha iniciado sesión (web/sesiones.py). Un comercio solo ve lo suyo: sin selector de comercios,
// sin la web de modelos ni el modo demostración (el servidor lo impone igualmente)
function aplicarSesion(s) {
  estado.sesion = s;
  estado.usuario = s.nombre;
  document.getElementById('sesion-nombre').textContent = s.nombre;
  document.getElementById('barra-invitado').hidden = !s.invitado;      // modo invitado: solo mirar
  // La pestaña Cuentas (cuentas.js) y la web de modelos, solo para el administrador de la entidad
  document.getElementById('nav-cuentas-boton').hidden = s.rol !== 'administrador';
  document.getElementById('ir-modelos').hidden = s.rol !== 'administrador';
  if (s.rol === 'comercio') {
    estado.comercio = String(s.comercio_id);
    estado.demo = false;
    ['comercio', 'zona-demo'].forEach((id) => { document.getElementById(id).hidden = true; });
  }
}
document.getElementById('salir-invitado').addEventListener('click', () => document.getElementById('cerrar-sesion').click());
document.getElementById('cerrar-sesion').addEventListener('click', async () => {
  await fetch('/api/logout', { method: 'POST' }).catch(() => {});
  location.href = '/login';
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
    aplicarSesion(await api('/api/sesion'));
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
