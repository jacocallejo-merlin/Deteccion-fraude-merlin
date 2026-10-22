'use strict';
// Centinela · Modelos: informe para dirección. La pregunta de la pantalla: "¿Qué modelo usamos y cuánto fraude
// encuentra?". La responde el bloque más grande (los 201 fraudes del examen, marcados por cada modelo) y su cifra,
// la mayor de la pantalla. Todo se dibuja desde /api/modelos; los totales salen de las filas.

const nf = new Intl.NumberFormat('es-ES');
const num = (n) => nf.format(n);
const pct = (v) => `${nf.format(Math.round(v))} %`;
const fechaCorta = (s) => new Date(`${s}T00:00:00`).toLocaleDateString('es-ES', { day: 'numeric', month: 'short' }).replace('.', '');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const color = (nombre) => getComputedStyle(document.documentElement).getPropertyValue(nombre).trim();

const vistaInicial = new URLSearchParams(location.search).get('vista') === 'avisos' ? 'avisos' : 'fraude';
const estado = { datos: null, vista: vistaInicial, modelo: new URLSearchParams(location.search).get('modelo') || '' };

// ------------------------------------------------------------------------------ cifras derivadas
// Una cifra que sale dos veces es una sola variable: todo lo que se repite en la pantalla se calcula aquí
function derivar(d) {
  const elegido = d.modelos.find((m) => m.elegido) || d.modelos[0];
  const empatados = d.modelos.filter((m) => m !== elegido && m.encontrados === elegido.encontrados);
  const acierto = (m) => (m.encontrados / m.avisos) * 100;                 // de cada 100 avisos, cuántos son fraude
  const semanas = d.semanas.filter((s) => s.completa);
  const aprendiendo = semanas.filter((s) => s.semana >= 2);               // la semana 1 aún no ha aprendido nada
  const media = (k) => aprendiendo.reduce((t, s) => t + s[k], 0) / Math.max(1, aprendiendo.length);
  const mejora = aprendiendo.length ? Math.round((media('aprendiendo') / media('sin_aprender') - 1) * 100) : null;
  const versiones = d.proceso.versiones_clasicos + d.proceso.versiones_red;
  const puestoValidacion = [...d.modelos].sort((a, b) => b.nota_validacion - a.nota_validacion).indexOf(elegido) + 1;
  console.assert(d.modelos.every((m) => m.encontrados <= d.fraudes), 'ningún modelo encuentra más fraudes de los que hay');
  console.assert(versiones === d.proceso.versiones_clasicos + d.proceso.versiones_red, 'versiones = clásicas + red');
  return { elegido, empatados, acierto, semanas, aprendiendo, mejora, versiones, puestoValidacion };
}

// ------------------------------------------------------------------------------ título, sello y cifras del proceso
function pintarCabecera(d, x) {
  const e = x.elegido;
  document.getElementById('hallazgo').innerHTML =
    `Usamos <b>${esc(e.nombre)}</b>: encuentra <b>${num(e.encontrados)} de los ${num(d.fraudes)} fraudes</b> del examen
    avisando de unos ${num(Math.round(e.avisos_dia))} pagos al día${x.mejora != null ? `, y desde que aprende cada semana acierta un ${num(x.mejora)} % más` : ''}.`;
  document.getElementById('sello').textContent = `Examen final del ${fechaCorta(d.desde)} al ${fechaCorta(d.hasta)}`;
  document.getElementById('t-porque').textContent = `Por qué usamos ${e.nombre}`;
}

// Cómo lo hicimos: los pasos del proyecto, de los datos al modelo en uso
function pintarProceso(d, x) {
  const p = d.proceso;
  const pasos = [
    ['Datos', 'Pagos simulados con 10 tipos de fraude', `${num(p.dias_datos)} días`],
    ['Aprender', 'Pagos con los que cada modelo aprendió lo normal', num(p.pagos_aprender)],
    ['Probar', `${p.versiones_clasicos} versiones de los modelos clásicos y ${p.versiones_red} de la red`, num(x.versiones)],
    ['Examen final', `Pagos nunca vistos, con ${num(d.fraudes)} fraudes`, num(d.pagos)],
    ['En uso', 'Y aprende cada semana', x.elegido.nombre],
  ];
  document.getElementById('proceso').innerHTML = pasos.map(([titulo, detalle, cifra]) =>
    `<li><span><b>${esc(titulo)}</b>${esc(detalle)}</span><strong>${esc(cifra)}</strong></li>`).join('');
}

// ------------------------------------------------------------------------------ el bloque principal
// Cada marca es uno de los 201 fraudes (vista "fraude") o uno de cada 100 avisos (vista "avisos"); el modelo
// colorea los que acierta, en orden. El que usamos lleva el verde; los demás, tinta. Lo no acertado, gris claro.
// tam: el tamaño de cada marca; en la ficha de un modelo, donde hay sitio, más grandes
function marcas(total, llenas, elegido, ancho, encender = false, tam = { lado: 5, alto: 12, hueco: 2 }) {
  const { lado, hueco } = tam;
  const alto = tam.alto;
  const columnas = Math.max(20, Math.floor((ancho + hueco) / (lado + hueco)));
  const filas = Math.ceil(total / columnas);
  const relleno = elegido ? color('--accent') : color('--marca-otro');
  const vacio = color('--marca-vacia');
  let rects = '';
  for (let i = 0; i < total; i += 1) {
    const cx = (i % columnas) * (lado + hueco);
    const cy = Math.floor(i / columnas) * (alto + hueco);
    const lleno = i < llenas;
    rects += `<rect x="${cx}" y="${cy}" width="${lado}" height="${alto}" rx="1" fill="${lleno ? relleno : vacio}"${lleno && encender ? ` class="lleno" style="--i:${i}"` : ''}/>`;
  }
  const w = columnas * (lado + hueco) - hueco;
  const h = filas * (alto + hueco) - hueco;
  return `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true"${encender ? ' class="encender"' : ''}>${rects}</svg>`;
}

function pintarPrincipal(d, x) {
  const e = x.elegido;
  const fraude = estado.vista === 'fraude';
  const lider = document.getElementById('lider');
  lider.innerHTML = fraude
    ? `<b>${num(e.encontrados)}<small>de ${num(d.fraudes)} fraudes</small></b>
       <span>los encuentra ${esc(e.nombre)}${x.empatados.length ? `, igual que ${x.empatados.map((m) => esc(m.nombre)).join(' y ')}` : ''}</span>`
    : `<b>${num(Math.round(x.acierto(e)))}<small>de cada 100 avisos</small></b>
       <span>de ${esc(e.nombre)} son fraude de verdad; el resto, falsas alarmas</span>`;
  const relleno = color('--accent');
  const otro = color('--marca-otro');
  const vacio = color('--marca-vacia');
  document.getElementById('leyenda-marcas').innerHTML = fraude
    ? `<span><i class="muestra" style="background:${relleno}"></i><i class="muestra" style="background:${otro}"></i>Cada marca es uno de los ${num(d.fraudes)} fraudes: coloreados, los que encuentra</span>
       <span><i class="muestra" style="background:${vacio}"></i>Los que se le escapan</span>`
    : `<span><i class="muestra" style="background:${relleno}"></i><i class="muestra" style="background:${otro}"></i>De cada 100 avisos, los que eran fraude</span>
       <span><i class="muestra" style="background:${vacio}"></i>Falsas alarmas</span>`;
  const ranking = document.getElementById('ranking');
  const ancho = Math.max(180, ranking.clientWidth - (window.matchMedia('(max-width: 860px)').matches ? 0 : 224 + 104 + 40));
  ranking.innerHTML = d.modelos.map((m) => {
    const llenas = fraude ? m.encontrados : Math.round(x.acierto(m));
    const total = fraude ? d.fraudes : 100;
    const valor = fraude
      ? `<b>${num(m.encontrados)}</b><small>de ${num(d.fraudes)}</small>`
      : `<b>${num(Math.round(x.acierto(m)))}</b><small>de cada 100</small>`;
    return `<div class="fila-modelo" data-modelo="${m.clave}" role="button" tabindex="0" aria-label="Ver la ficha de ${esc(m.nombre)}">
      <div class="texto"><div class="nombre"><span translate="no">${esc(m.nombre)}</span>${m.elegido ? '<span class="usado">El que usamos</span>' : ''}</div>
        <p class="idea">${esc(m.idea)}</p><span class="ver">Ver ficha</span></div>
      <div class="marcas" role="img" aria-label="${esc(m.nombre)}: ${fraude ? `encuentra ${m.encontrados} de ${d.fraudes} fraudes` : `${Math.round(x.acierto(m))} de cada 100 avisos son fraude`}">${marcas(total, llenas, m.elegido, ancho)}</div>
      <div class="valor">${valor}</div>
    </div>`;
  }).join('');
}

// ------------------------------------------------------------------------------ por qué este modelo
function pintarRazones(d, x) {
  const e = x.elegido;
  const razones = [
    ['Encuentra más fraude', x.empatados.length ? `Empata con ${x.empatados.map((m) => m.nombre).join(' y ')}` : 'El que más de los cinco', `${num(e.encontrados)} de ${num(d.fraudes)}`],
    ['Mejor en la prueba previa', 'Antes del examen, para elegir', `${x.puestoValidacion}.º de ${d.modelos.length}`],
    ['Frente a avisar al azar', 'Con los mismos avisos', `×${num(Math.round(e.veces_azar))}`],
  ];
  const grupos = d.proceso.parametros?.[e.clave]?.k;
  if (grupos) razones.push(['El más sencillo de explicar', 'Pocos grupos de pagos normales', `${num(grupos)} grupos`]);
  document.getElementById('razones').innerHTML = razones.map(([titulo, detalle, valor]) =>
    `<div><dt>${esc(titulo)}<small>${esc(detalle)}</small></dt><dd>${esc(valor)}</dd></div>`).join('');
}

// ------------------------------------------------------------------------------ qué fraude ve cada uno
function pintarMatriz(d) {
  const tipos = [...d.tipos.filter((t) => !t.nuevo), ...d.tipos.filter((t) => t.nuevo)];   // el fraude nuevo, al final
  const corto = { kmeans: 'K-Means', ocsvm: 'SVM', dbscan: 'DBSCAN', iforest: 'I. Forest', autoencoder: 'Autoenc.' };
  const cab = `<thead><tr><th>Tipo de fraude</th>${d.modelos.map((m) =>
    `<th class="n${m.elegido ? ' elegido' : ''}" title="${esc(m.nombre)}">${esc(corto[m.clave] || m.nombre)}</th>`).join('')}</tr></thead>`;
  const filas = tipos.map((t) => `<tr>
      <td class="tipo">${esc(t.nombre)}<small class="${t.nuevo ? 'nuevo' : ''}">${t.nuevo ? `${num(t.casos)} casos. Nuevo: no salía al entrenar` : `${num(t.casos)} casos`}</small></td>
      ${d.modelos.map((m) => {
        const v = t.encontrado[m.clave] ?? 0;
        return `<td class="n${m.elegido ? ' elegido' : ''}">${pct(v)}<span class="pista" aria-hidden="true"><i style="width:${v}%"></i></span></td>`;
      }).join('')}
    </tr>`).join('');
  document.getElementById('matriz').innerHTML = `${cab}<tbody>${filas}</tbody>`;
}

// ------------------------------------------------------------------------------ aprende cada semana
function pintarSemanas(d, x) {
  const caja = document.getElementById('g-semanas');
  if (x.semanas.length < 2) {
    caja.innerHTML = '<p class="empty">Aún no hay dos semanas completas de aprendizaje.</p>';
    return;
  }
  document.getElementById('lider-semanas').innerHTML = x.mejora != null
    ? `<b>+${num(x.mejora)} %</b><span>más avisos acertados que sin aprender</span>` : '';
  const dibujar = (alto) => Charts.line(caja, {
    labels: x.semanas.map((s) => `Semana ${s.semana}`),
    series: [
      { name: 'Aprendiendo cada semana', values: x.semanas.map((s) => s.aprendiendo) },
      { name: 'Sin aprender', values: x.semanas.map((s) => s.sin_aprender), dash: true },
    ],
    format: (v) => `${nf.format(Math.round(v))} %`,
    yTicks: [0, 20, 40, 60],
    height: alto,
    legend: document.getElementById('leyenda-semanas'),
  });
  dibujar(alturaSemanas());
  // La leyenda aparece al dibujar: se vuelve a medir una vez para que la columna acabe justo al nivel
  const ajustada = alturaSemanas();
  if (Math.abs(ajustada - caja.getBoundingClientRect().height) > 2) dibujar(ajustada);
}

// Lo que le falta a la columna de la derecha para acabar con el bloque principal, sin bajar de 140px
function alturaSemanas() {
  if (window.matchMedia('(max-width: 1100px)').matches) return 180;
  const principal = document.querySelector('.panel.principal').getBoundingClientRect().height;
  const pila = document.querySelector('.pila').getBoundingClientRect().height;
  const actual = document.getElementById('g-semanas').getBoundingClientRect().height;
  return Math.max(140, Math.round(actual + principal - pila));
}

// ------------------------------------------------------------------------------ la ficha de un modelo
// Cómo "piensa" cada modelo, en un dibujo: puntos grises son pagos normales; el rojo, el pago raro que avisa
const TINTA = '#3b424b';
const PUNTO = '#aab3bd';
const RARO = '#d33a2c';
function puntos(cx, cy, n, radio, semilla) {
  let s = semilla;
  const azar = () => { s = (s * 9301 + 49297) % 233280; return s / 233280; };
  return Array.from({ length: n }, () => {
    const a = azar() * Math.PI * 2;
    const r = Math.sqrt(azar()) * radio;
    return [cx + Math.cos(a) * r, cy + Math.sin(a) * r * 0.8];
  });
}
const pintarPuntos = (lista) => lista.map(([x, y]) => `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3.2" fill="${PUNTO}"/>`).join('');
const raro = (x, y) => `<circle cx="${x}" cy="${y}" r="5.5" fill="${RARO}"/>`;
const DIBUJOS = {
  kmeans: () => [[52, 40], [104, 86], [150, 38]].map(([x, y], i) =>
    `<circle cx="${x}" cy="${y}" r="24" fill="none" stroke="${TINTA}" stroke-width="1.2" stroke-dasharray="3 3"/>${pintarPuntos(puntos(x, y, 8, 17, i + 3))}`).join('')
    + `<line x1="128" y1="86" x2="176" y2="104" stroke="${RARO}" stroke-width="1.2" stroke-dasharray="3 3"/>${raro(182, 106)}`,
  ocsvm: () => `<path d="M30 62 C30 28 70 18 104 22 C142 26 164 44 160 70 C156 96 118 104 86 100 C52 96 30 90 30 62 Z" fill="none" stroke="${TINTA}" stroke-width="1.4"/>`
    + pintarPuntos(puntos(96, 62, 22, 46, 11)) + raro(182, 22),
  dbscan: () => pintarPuntos(puntos(58, 64, 13, 22, 5)) + pintarPuntos(puntos(128, 78, 11, 20, 8))
    + `<circle cx="168" cy="28" r="20" fill="none" stroke="${RARO}" stroke-width="1.2" stroke-dasharray="3 3"/>${raro(168, 28)}`,
  iforest: () => `<rect x="12" y="10" width="176" height="100" fill="none" stroke="#ccd2d9"/>` + pintarPuntos(puntos(76, 66, 22, 48, 17))
    + `<line x1="146" y1="10" x2="146" y2="110" stroke="${TINTA}" stroke-width="1.4"/>`
    + `<line x1="146" y1="46" x2="188" y2="46" stroke="${TINTA}" stroke-width="1.4"/>${raro(168, 28)}`,
  autoencoder: () => {
    const capas = [[24, [22, 46, 70, 94]], [100, [46, 70]], [176, [22, 46, 70, 94]]];
    let lineas = '';
    for (let c = 0; c < 2; c += 1) {
      capas[c][1].forEach((y1) => capas[c + 1][1].forEach((y2) => {
        lineas += `<line x1="${capas[c][0]}" y1="${y1}" x2="${capas[c + 1][0]}" y2="${y2}" stroke="#ccd2d9" stroke-width="1"/>`;
      }));
    }
    return lineas + capas.flatMap(([x, ys], c) => ys.map((y, i) => (c === 2 && i === 2
      ? raro(x, y) : `<circle cx="${x}" cy="${y}" r="6" fill="${c === 1 ? TINTA : PUNTO}"/>`))).join('');
  },
};

// Los parámetros con los que se quedó cada modelo, dichos en palabras
const dec = (v) => new Intl.NumberFormat('es-ES', { maximumFractionDigits: 3 }).format(v);
const PARAMETROS = {
  kmeans: (p) => [['Grupos de pagos normales', num(p.k)]],
  ocsvm: (p) => [['Pagos normales que deja fuera de la frontera', `${dec(p.nu * 100)} %`], ['Lo ceñida que es la frontera', dec(p.gamma)]],
  dbscan: (p) => [['Distancia para ser vecinos', dec(p.eps)], ['Vecinos que necesita para no estar solo', num(p.min_samples)]],
  iforest: (p) => [['Árboles de preguntas', num(p.n_estimators)], ['Pagos que mira cada árbol', num(p.max_samples)]],
  autoencoder: (p) => [['Números a los que resume cada pago', num(p.cuello)], ['Rondas de entrenamiento', num(p.rondas)],
    ['Capas de la red', String(p.arquitectura || '').replaceAll('->', '→')]],
};

const GRANDES = { lado: 8, alto: 18, hueco: 3 };
function pintarModelo(d, x, m, animar) {
  const total = d.fraudes;
  const acierto = Math.round(x.acierto(m));
  const e = x.elegido;
  const dif = m.encontrados - e.encontrados;
  const frente = m === e ? `Es el modelo que usamos en Centinela.`
    : dif === 0 ? `Encuentra los mismos fraudes que ${e.nombre}, el que usamos.`
      : `Encuentra ${num(Math.abs(dif))} fraudes ${dif < 0 ? 'menos' : 'más'} que ${e.nombre}, el que usamos.`;
  const puesto = [...d.modelos].sort((a, b) => b.nota_validacion - a.nota_validacion).indexOf(m) + 1;
  const tipos = d.tipos.map((t) => ({ ...t, v: t.encontrado[m.clave] ?? 0 })).sort((a, b) => b.v - a.v);
  const mejores = tipos.filter((t) => t.v >= 70).map((t) => t.nombre.toLowerCase());
  const ciegos = tipos.filter((t) => t.v === 0).length;
  const columna = document.querySelector('.columna').clientWidth;
  const ancho = window.matchMedia('(max-width: 860px)').matches ? columna - 48 : Math.max(200, Math.min(700, (columna * 8) / 12 - 48));
  const ent = m.entrenamiento || { versiones: 0, parametros: {} };
  const parametros = (PARAMETROS[m.clave] || (() => []))(ent.parametros || {});
  const vista = document.getElementById('vista-modelo');
  vista.innerHTML = `
    <section class="panel principal ficha${m.elegido ? ' elegido' : ''}" aria-labelledby="t-ficha">
      <header>
        <div><h2 id="t-ficha" translate="no">${esc(m.nombre)}</h2><p class="meta">Familia: ${esc(m.familia)}</p></div>
        ${m.elegido ? '<span class="pill pos">El que usamos</span>' : ''}
      </header>
      <div class="ficha-explica">
        <div>
          <h3>Cómo funciona</h3>
          <p>${esc(m.como_funciona)}</p>
          <p>En el examen encontró <b>${num(m.encontrados)} de los ${num(total)} fraudes</b> (${pct((m.encontrados / total) * 100)}) avisando de unos
            ${num(Math.round(m.avisos_dia))} pagos al día. ${esc(frente)}</p>
        </div>
        <svg viewBox="0 0 200 120" role="img" aria-label="Dibujo de cómo busca lo raro ${esc(m.nombre)}">${(DIBUJOS[m.clave] || (() => ''))()}</svg>
      </div>
      <div class="kpis ficha-cifras">
        <div class="kpi"><div class="kpi-label">Fraudes encontrados</div><div class="kpi-value">${num(m.encontrados)} <small>de ${num(total)}</small></div></div>
        <div class="kpi"><div class="kpi-label">Avisos que aciertan</div><div class="kpi-value">${num(acierto)} <small>de cada 100</small></div></div>
        <div class="kpi"><div class="kpi-label">Frente al azar</div><div class="kpi-value">×${num(Math.round(m.veces_azar))}</div></div>
        <div class="kpi"><div class="kpi-label">Prueba previa</div><div class="kpi-value">${puesto}.º <small>de ${d.modelos.length}</small></div></div>
      </div>
      <div class="ficha-marcas">
        <h3>Los ${num(total)} fraudes del examen</h3>
        <p class="leyenda-marcas"><span><i class="muestra" style="background:${m.elegido ? color('--accent') : color('--marca-otro')}"></i>Los que encuentra</span>
          <span><i class="muestra" style="background:${color('--marca-vacia')}"></i>Los que se le escapan</span></p>
        <div class="marcas" role="img" aria-label="Encuentra ${m.encontrados} de ${total} fraudes">${marcas(total, m.encontrados, m.elegido, ancho, animar, GRANDES)}</div>
        <h3>De cada 100 avisos</h3>
        <p class="leyenda-marcas"><span>Los coloreados eran fraude; el resto, falsas alarmas</span></p>
        <div class="marcas" role="img" aria-label="${acierto} de cada 100 avisos son fraude">${marcas(100, acierto, m.elegido, ancho, animar, GRANDES)}</div>
      </div>
      <div class="ficha-frente">
        <h3>Frente a los demás</h3>
        <ul class="frente">${d.modelos.map((o) => `
          <li><button type="button" data-ir="${o.clave}"${o === m ? ' aria-current="true"' : ''}>
            <span translate="no">${esc(o.nombre)}</span>
            <span class="pista" aria-hidden="true"><i style="width:${(o.encontrados / total) * 100}%"></i></span>
            <b>${num(o.encontrados)}</b></button></li>`).join('')}</ul>
      </div>
    </section>
    <div class="pila lateral">
      <section class="panel" aria-labelledby="t-ve">
        <header><h2 id="t-ve">Qué fraude ve</h2><span class="meta">Parte de cada tipo</span></header>
        <ul class="barras-tipo${m.elegido ? ' elegido' : ''}">${tipos.map((t) => `
          <li><span>${esc(t.nombre)}${t.nuevo ? ' <small>nuevo</small>' : ''}</span><b>${pct(t.v)}</b>
            <span class="pista" aria-hidden="true"><i style="width:${t.v}%"></i></span></li>`).join('')}</ul>
        <p class="ficha-nota">${mejores.length ? `Lo que mejor ve: ${esc(mejores.slice(0, 3).join(', '))}. ` : ''}${ciegos ? `No encuentra ninguno de ${ciegos} tipos.` : ''}</p>
      </section>
      <section class="panel" aria-labelledby="t-entreno">
        <header><h2 id="t-entreno">Cómo se entrenó</h2></header>
        <dl class="razones">
          <div><dt>Versiones probadas<small>Para quedarse con la mejor</small></dt><dd>${num(ent.versiones)}</dd></div>
          ${parametros.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join('')}
        </dl>
      </section>
    </div>`;
}

// ------------------------------------------------------------------------------ pestañas: resumen o un modelo
function pintarPestanas(d) {
  const lista = document.getElementById('pestanas');
  lista.innerHTML = `<button type="button" role="tab" data-modelo="" aria-controls="vista-resumen">Resumen</button>`
    + d.modelos.map((m) => `<button type="button" role="tab" data-modelo="${m.clave}" aria-controls="vista-modelo" translate="no">${esc(m.nombre)}${m.elegido ? '<span class="usado-punto" title="El que usamos"></span>' : ''}</button>`).join('');
  marcarPestana(false);
}

function marcarPestana(animar) {
  document.querySelectorAll('#pestanas [role="tab"]').forEach((b) => {
    const activa = b.dataset.modelo === (estado.modelo || '');
    b.setAttribute('aria-selected', String(activa));
    b.tabIndex = activa ? 0 : -1;
  });
  const activa = document.querySelector('#pestanas [aria-selected="true"]');
  const sub = document.getElementById('subrayado');
  if (!activa) return;
  sub.classList.toggle('sin-transicion', !animar);
  const lista = document.getElementById('pestanas');
  // En el móvil la barra se desplaza: la pestaña elegida se trae a la vista
  if (activa.offsetLeft < lista.scrollLeft || activa.offsetLeft + activa.offsetWidth > lista.scrollLeft + lista.clientWidth) {
    lista.scrollLeft = Math.max(0, activa.offsetLeft - 16);
  }
  sub.style.transform = `translateX(${activa.offsetLeft - lista.scrollLeft}px) scaleX(${activa.offsetWidth})`;
}

// Cambiar de vista: la que se va desaparece rápido y la nueva entra subiendo 8px (Emil: salida más corta que
// la entrada; opacidad y transform; ease-out fuerte). Con el teclado o con "reducir movimiento", sin viaje.
const reducir = window.matchMedia('(prefers-reduced-motion: reduce)');
async function abrir(modelo, { animar = true, historial = true } = {}) {
  if (!estado.datos || (modelo || '') === (estado.modelo || '')) return;
  const anterior = document.getElementById(estado.modelo ? 'vista-modelo' : 'vista-resumen');
  estado.modelo = modelo || '';
  if (historial) {
    const url = new URL(location.href);
    if (estado.modelo) url.searchParams.set('modelo', estado.modelo); else url.searchParams.delete('modelo');
    history.pushState({ modelo: estado.modelo }, '', url);
  }
  marcarPestana(animar);
  const movimiento = animar && !reducir.matches;
  if (animar) {
    // Se espera a que acabe la salida, pero nunca más de 150 ms (en una pestaña en segundo plano el navegador
    // congela las animaciones y la vista no debe quedarse a medias)
    const salida = anterior.animate([{ opacity: 1 }, { opacity: 0 }], { duration: 120, easing: 'ease-out' });
    await Promise.race([salida.finished.catch(() => {}), new Promise((r) => setTimeout(r, 150))]);
    salida.cancel();
  }
  mostrarVista(animar);
  const nueva = document.getElementById(estado.modelo ? 'vista-modelo' : 'vista-resumen');
  if (animar) {
    nueva.animate(movimiento
      ? [{ opacity: 0, transform: 'translateY(8px)' }, { opacity: 1, transform: 'none' }]
      : [{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: 'cubic-bezier(0.23, 1, 0.32, 1)' });
  }
}

function mostrarVista(animar = false) {
  const d = estado.datos;
  const m = d.modelos.find((x) => x.clave === estado.modelo);
  if (!m) estado.modelo = '';
  document.getElementById('vista-resumen').hidden = Boolean(m);
  document.getElementById('vista-modelo').hidden = !m;
  document.getElementById('vista-resumen').style.opacity = '';
  if (m) pintarModelo(d, derivar(d), m, animar && !reducir.matches);
  else pintarSemanas(d, derivar(d));       // la altura de la gráfica depende de la vista ya visible
}

// ------------------------------------------------------------------------------ estados
function error(mensaje) {
  const bloque = `<p class="error-bloque">No se han podido cargar los resultados. ${esc(mensaje)}
    <button class="btn" type="button" data-reintentar>Reintentar</button></p>`;
  document.getElementById('hallazgo').textContent = 'Sin datos de los modelos.';
  document.getElementById('lider').innerHTML = '';
  document.getElementById('ranking').innerHTML = bloque;
  ['razones', 'g-semanas', 'proceso'].forEach((id) => { document.getElementById(id).innerHTML = ''; });
  document.getElementById('matriz').innerHTML = '';
}

function pintarTodo() {
  const d = estado.datos;
  const x = derivar(d);
  pintarCabecera(d, x);
  pintarPrincipal(d, x);
  pintarRazones(d, x);
  pintarMatriz(d);
  pintarProceso(d, x);
  pintarPestanas(d);
  mostrarVista(false);
}

async function cargar() {
  try {
    const r = await fetch('api/modelos');   // relativa: funciona en / y colgando de /modelos/
    const datos = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(datos.detail || `Error ${r.status}`);
    estado.datos = datos;
    // La web de casos: la que diga el servidor o, si no, la misma máquina en el puerto 8000
    document.getElementById('ir-casos').href = datos.web_casos || `${location.protocol}//${location.hostname}:8000/`;
    if (document.fonts && document.fonts.ready) await document.fonts.ready;   // las etiquetas se miden con la fuente cargada
    pintarTodo();
  } catch (e) {
    error(e.message);
  }
}

// ------------------------------------------------------------------------------ controles
document.querySelector('.segmented').addEventListener('click', (ev) => {
  const b = ev.target.closest('button[data-vista]');
  if (!b || !estado.datos) return;
  estado.vista = b.dataset.vista;
  marcarVista();
  const url = new URL(location.href);
  if (estado.vista === 'fraude') url.searchParams.delete('vista'); else url.searchParams.set('vista', estado.vista);
  history.replaceState(null, '', url);
  pintarPrincipal(estado.datos, derivar(estado.datos));
});
function marcarVista() {
  document.querySelectorAll('.segmented button').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.vista === estado.vista)));
}
marcarVista();
document.getElementById('pdf').addEventListener('click', () => window.print());

// Pestañas: clic con transición; flechas del teclado sin ella (lo que se hace con el teclado no se anima)
const pestanas = document.getElementById('pestanas');
pestanas.addEventListener('click', (ev) => {
  const b = ev.target.closest('[role="tab"]');
  if (b) abrir(b.dataset.modelo);
});
pestanas.addEventListener('keydown', (ev) => {
  const paso = { ArrowRight: 1, ArrowLeft: -1 }[ev.key];
  if (!paso) return;
  ev.preventDefault();
  const botones = [...pestanas.querySelectorAll('[role="tab"]')];
  const i = botones.findIndex((b) => b.getAttribute('aria-selected') === 'true');
  const siguiente = botones[(i + paso + botones.length) % botones.length];
  abrir(siguiente.dataset.modelo, { animar: false });
  siguiente.focus();
});
pestanas.addEventListener('scroll', () => marcarPestana(false), { passive: true });
// En la ficha, "Frente a los demás" lleva a la ficha de cada modelo
document.getElementById('vista-modelo').addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-ir]');
  if (b && b.dataset.ir !== estado.modelo) abrir(b.dataset.ir);
});
// En el resumen, cada fila abre la ficha de su modelo
document.getElementById('ranking').addEventListener('click', (ev) => {
  const f = ev.target.closest('.fila-modelo[data-modelo]');
  if (f) { abrir(f.dataset.modelo); window.scrollTo({ top: 0, behavior: reducir.matches ? 'auto' : 'smooth' }); }
});
document.getElementById('ranking').addEventListener('keydown', (ev) => {
  const f = ev.target.closest('.fila-modelo[data-modelo]');
  if (f && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); abrir(f.dataset.modelo, { animar: false }); }
});
// Atrás y adelante del navegador vuelven a la vista anterior
window.addEventListener('popstate', () => {
  const modelo = new URLSearchParams(location.search).get('modelo') || '';
  abrir(modelo, { historial: false });
});
document.addEventListener('click', (ev) => { if (ev.target.closest('[data-reintentar]')) cargar(); });
let espera;
window.addEventListener('resize', () => {
  clearTimeout(espera);
  espera = setTimeout(() => { if (estado.datos) pintarTodo(); }, 150);
});

cargar();
