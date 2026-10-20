# Herramienta antifraude: detección de anomalías en transacciones

Este proyecto es un sistema que **busca pagos fraudulentos** entre miles de pagos normales y avisa
de los más sospechosos para que una persona los revise.

Este documento está pensado para que **cualquiera lo entienda, aunque no sepa nada de fraude ni de
inteligencia artificial**. Primero explica la idea, después cómo funciona por dentro y al final
cómo ejecutarlo.

## Resumen ejecutivo

- **Qué hace:** da una nota de sospecha a cada pago y ordena las alertas para que el equipo revise
  solo el 1 % más sospechoso (unas 6-7 al día), en vez de revisar los ~650 pagos diarios.
- **Cómo:** cuatro detectores no supervisados (Isolation Forest, KMeans, DBSCAN y One-Class SVM) y
  un autoencoder semi-supervisado (se entrena solo con pagos legítimos); KMeans es el detector base y
  un modelo supervisado recalibra el orden cada semana con lo que confirman el banco y los analistas.
- **Datos:** unos 117.000 pagos sintéticos en 180 días (116.547 tras limpiar), 2.000 clientes,
  30 comercios, 616 pagos fraudulentos (0,53 %) y 10 tipos de fraude, incluido uno que el sistema no
  ha visto nunca.
- **Resultado principal (test: 29.174 pagos, 201 fraudes):** KMeans obtiene una PR-AUC de 0,41
  (unas 60 veces el azar) y encuentra el 42 % del fraude revisando el 1 % (el umbral de validación
  marca un 1,1 % real en test); de cada 100 alertas, unas 26 son fraude.
- **Aprendizaje (Hito 6):** en los mismos días y con la misma carga de revisión, el modelo
  recalibrado con los fraudes que confirma el banco encuentra 98 fraudes frente a 70 de KMeans
  (+40 %).
- **Límite principal:** el fraude que imita a un cliente normal (`cash_out_multicomercio`) apenas
  se detecta: 0 % en el detector no supervisado y 16 % en la cola tras el ajuste del Hito 6.

---

## Índice

- [Resumen ejecutivo](#resumen-ejecutivo)

**1. Entender el proyecto**
- [El problema](#el-problema)
- [La idea en una frase](#la-idea-en-una-frase)
- [Cómo funciona, paso a paso](#cómo-funciona-paso-a-paso)
- [Diccionario](#diccionario)

**2. Lo que hay dentro**
- [Los datos](#los-datos)
- [Los modelos: cinco formas de buscar lo raro](#los-modelos-cinco-formas-de-buscar-lo-raro)
- [Los analistas y el aprendizaje semanal](#los-analistas-y-el-aprendizaje-semanal)
- [La automatización](#la-automatización)

**3. Resultados**
- [Cómo sabemos que funciona (y no nos engañamos)](#cómo-sabemos-que-funciona-y-no-nos-engañamos)
- [Qué ha salido](#qué-ha-salido)
- [Lo que no hace bien (limitaciones)](#lo-que-no-hace-bien-limitaciones)
- [Preguntas que os van a hacer](#preguntas-que-os-van-a-hacer)

**4. Usarlo**
- [Ejecutarlo](#ejecutarlo)
- [Qué mirar después de ejecutar](#qué-mirar-después-de-ejecutar)
- [Mapa del proyecto](#mapa-del-proyecto)
- [Correspondencia con los hitos](#correspondencia-con-los-hitos)

---

# 1. Entender el proyecto

## El problema

Cada día un banco procesa miles de pagos con tarjeta. Casi todos son normales: alguien compra en el
supermercado, paga una cena o compra ropa por internet. Pero **más o menos 1 de cada 200 es
fraude**: alguien que ha robado una tarjeta o ha entrado en la cuenta de otra persona.

¿Por qué es difícil encontrarlos?

- **Son muy pocos.** Si revisas pagos al azar, casi siempre encontrarás pagos normales.
- **No se pueden revisar todos.** Con 650 pagos al día, el equipo de fraude solo tiene tiempo para
  mirar unos pocos.
- **Los estafadores cambian.** Si solo buscas lo que ya conoces ("compras de madrugada desde otro
  país"), se te escapa el fraude nuevo que nadie ha descrito todavía.
- **Lo raro no siempre es fraude.** Alguien que se compra un sofá de 2.000 € a las 3 de la mañana
  es raro, pero no es un delincuente.

## La idea en una frase

> **El sistema da a cada pago una "nota de sospecha" y el equipo de fraude solo revisa los pagos
> con la nota más alta.**

Funciona como un **detector de humo**: no sabe qué es un incendio, pero sabe cómo es el aire normal
y pita cuando algo se sale de lo normal. Después una persona va a ver si de verdad hay fuego.

Y como un buen detector, **aprende**: cada semana mira qué alarmas eran fuego de verdad y cuáles
eran una tostada quemada, y ajusta cuándo pitar.

## Cómo funciona, paso a paso

```
  1. LLEGAN         2. SE LIMPIAN      3. SE DESCRIBEN      4. SE PUNTÚAN
  los pagos    ──►  (errores,     ──►  (~35 datos por  ──►  (nota de sospecha
  del día           duplicados)        cada pago)           con KMeans)
                                                                 │
                                                                 ▼
  7. CADA SEMANA        6. LOS ANALISTAS          5. SE ELIGEN LAS ALERTAS
  el sistema    ◄────── dicen si era      ◄────── (el 1 % con la nota más
  aprende               fraude o no                 alta: unos 7 pagos al día)
     │
     └──► y la semana siguiente las alertas salen mejor ordenadas
```

1. **Llegan los pagos del día.** Cada pago trae: importe, fecha y hora, tarjeta, comercio, si fue
   por internet o en tienda, desde qué móvil u ordenador, desde qué país…
2. **Se limpian.** Los datos reales vienen sucios: pagos repetidos, fechas en formatos distintos,
   "España" escrito de tres formas, importes en dólares… Si no se arreglan, el sistema se equivoca.
3. **Se describen.** De cada pago se calculan unas 35 características que ayudan a ver si es
   sospechoso. Por ejemplo: *¿cuántas veces se ha usado esta tarjeta en los últimos 10 minutos?*,
   *¿este importe es normal para este cliente?*, *¿es un móvil que el cliente no había usado nunca?*
4. **Se puntúan.** Un modelo (KMeans) agrupa los pagos en perfiles y da a cada uno una nota según
   lo lejos que está del perfil más parecido: cuanto más raro, más alta.
5. **Se eligen las alertas.** El 1 % de pagos con la nota más alta (unos 7 al día) pasa a una lista
   de alertas.
6. **Los analistas revisan.** Una persona del equipo de fraude mira cada alerta y decide: ¿es
   fraude o es legítimo?
7. **El sistema aprende.** Cada semana, con lo que ya se sabe que era fraude (lo que dijeron los
   analistas y lo que confirmó el banco), se entrena un segundo modelo que reordena las alertas.

Todo esto se ejecuta **solo, cada día**, sin que nadie tenga que lanzarlo.

## Diccionario

**Sobre el negocio**

| Palabra | Qué significa |
|---|---|
| **Transacción** | Un pago. Usamos las dos palabras para lo mismo. |
| **Fraude** | Un pago que no ha hecho el dueño de la tarjeta (o que hace el dueño para estafar al banco). |
| **Legítimo** | Un pago normal, hecho por su dueño. |
| **Alerta** | Un pago que el sistema marca como sospechoso para que alguien lo revise. |
| **Analista** | La persona del equipo de fraude que revisa las alertas. |
| **Falsa alarma** (falso positivo) | Una alerta que al final era un pago normal. |
| **Contracargo** | Cuando el cliente reclama al banco "este pago no lo hice yo" y le devuelven el dinero. Es la forma en que el banco **confirma** que algo era fraude. Tarda días o semanas. |

**Sobre los modelos**

| Palabra | Qué significa |
|---|---|
| **Modelo** | Un programa que aprende de datos a hacer algo; aquí, a dar una nota de sospecha. |
| **Variable** (o *feature*) | Cada uno de los ~35 datos que describen un pago (importe, hora, pagos en la última hora…). |
| **Etiqueta** | La respuesta correcta: si un pago era fraude o no. En la vida real se sabe tarde y no siempre. |
| **Supervisado / no supervisado** | Un modelo *supervisado* aprende con ejemplos de la respuesta ("esto es fraude, esto no"). Uno *no supervisado* no usa esas respuestas: busca lo que se sale de lo normal. En el proyecto, los cuatro detectores clásicos son no supervisados, el autoencoder es semi-supervisado (se entrena solo con pagos legítimos) y el modelo que reordena las alertas es supervisado. |
| **Anomalía** | Algo que se sale de lo normal. No siempre es fraude. |
| **Entrenar** | Enseñar al modelo con datos del pasado. |
| **Semilla** | Un número que fija el azar. Con la misma semilla, el resultado sale siempre igual (así se puede repetir). |

**Sobre cómo se mide** (lo más importante para defender el proyecto)

| Palabra | Qué significa | Ejemplo |
|---|---|---|
| **Precisión** | De las alertas que revisas, ¿cuántas eran fraude? | 26 % = de cada 100 alertas, 26 eran fraude de verdad. |
| **Recall** (o cobertura) | Del fraude que hubo, ¿cuánto encontraste? | 42 % = de cada 100 fraudes, 42 llegaron a las alertas. |
| **PR-AUC** | Una nota de 0 a 1 que resume lo bien que el modelo pone el fraude arriba de la lista. 1 = perfecto. Al azar se saca lo que vale la proporción de fraude (0,007). | KMeans saca 0,41: unas 60 veces más que el azar. |
| **Entrenamiento / validación / test** | Los datos se parten por fechas, como un examen: con el **entrenamiento** se estudia, con la **validación** se elige el mejor modelo, y el **test** (los datos más recientes, que no se usan para entrenar ni elegir) es el examen final. | |
| **Intervalo de confianza** | El margen de error. Con pocos fraudes, una cifra puede variar por suerte. | PR-AUC entre 0,30 y 0,52. |

**Sobre la parte técnica**

| Palabra | Qué significa |
|---|---|
| **Pipeline** | La cadena de pasos (cargar → limpiar → describir → puntuar…) que se ejecutan uno detrás de otro. |
| **ClickHouse** | La base de datos donde se guardan todos los pagos y resultados. Es muy rápida con muchos datos. |
| **Docker** | Una "caja" que lleva el programa con todo lo que necesita, para que funcione igual en cualquier ordenador. |
| **Airflow** | Un programa que lanza el pipeline solo, cada día, y avisa si algo falla. Cada flujo de pasos se llama **DAG**. |
| **Kubernetes** | Un sistema para ejecutar esas "cajas" en servidores, de forma programada y escalable. |

---

# 2. Lo que hay dentro

## Los datos

### ¿Por qué son inventados?

Los bancos no comparten datos reales de sus clientes (son privados). Por eso el propio proyecto
**genera datos sintéticos**: inventados por un programa, pero que se comportan como los reales.
Lo hace el script `pipeline/dataset.py`.

### Qué contiene

- **2.000 clientes**, cada uno con 1 a 3 tarjetas y 1 a 3 dispositivos habituales (móvil, ordenador,
  tablet); los casos de dispositivo nuevo añaden uno más.
- **30 comercios** (tiendas online y físicas).
- **Unos 117.000 pagos en 180 días** (unos 650 al día).
- **Fraude: alrededor del 0,5 %** de los pagos (aproximadamente 1 de cada 200).
- Todo repartido en 13 tablas de origen: clientes, tarjetas, comercios, canales de pago,
  dispositivos, patrones, cliente-dispositivo, sesiones (cada vez que alguien entra en la web o la
  app), eventos (cada clic dentro de una sesión), pagos, devoluciones, alertas y analistas.

Cada cliente tiene su forma de comprar: unos gastan más, otros menos, unos compran por la mañana y
otros por la noche, algunos viajan, algunos usan VPN… Por eso el sistema incorpora el histórico de
cada cliente (por ejemplo, si el importe es normal para él o cuántas devoluciones ha hecho), aunque
el modelo agrupa todos los pagos en perfiles comunes.

### Los 10 tipos de fraude

| Tipo | Qué hace el estafador | En la vida real |
|---|---|---|
| `card_testing` | Hace muchas compras muy pequeñas seguidas | Ha robado tarjetas y prueba cuáles funcionan con compras de 1 € |
| `smurfing` | Reparte una cantidad grande en varias compras de poco menos de 1.000 € | Evita los controles que saltan a partir de 1.000 € |
| `account_takeover` | Entra en la cuenta de otro, cambia la contraseña y compra | Robo de cuenta |
| `ip_sospechosa` | Compra desde otro país, a menudo con VPN | Alguien en otro país usa los datos robados |
| `dispositivo_nuevo` | Compra desde un móvil que el cliente nunca había usado | El estafador usa su propio móvil |
| `pico_gasto` | Hace un gasto mucho mayor de lo normal para ese cliente | Aprovecha la tarjeta antes de que la bloqueen |
| `devolucion_abusiva` | Compra y devuelve una y otra vez | Abusa de las devoluciones |
| `bot` | Un programa hace clics a velocidad no humana | Compras automatizadas |
| `bust_out` | Se porta bien unos meses y de golpe gasta todo el límite de la tarjeta | "Me voy con todo y no pago" |
| `cash_out_multicomercio` | **Fraude desconocido** (ver abajo) | |

### Lo que hace que los datos sean difíciles (y realistas)

- **El fraude desconocido.** `cash_out_multicomercio` solo aparece en las últimas semanas. El
  sistema **nunca lo ha visto** y en el diseño inicial no se añadió ninguna variable ni regla
  pensando en él: es la prueba de si detecta fraude nuevo. Imita muy bien a un cliente normal: usa
  su móvil, su país e importes normales. Más tarde, el ajuste del Hito 6 añadió una variable
  (`n_comercios_cliente_3h`) que ayuda a separarlo.
- **Los clientes normales también hacen cosas raras.** Compras grandes de vez en cuando, viajes,
  un móvil nuevo, VPN, cambiar la contraseña… Así el sistema no puede hacer trampa pensando "todo lo
  raro es fraude".
- **El banco tarda en saber qué era fraude.** Como en la vida real, cada fraude se confirma con
  retraso (una mediana de 12 días, cuando el cliente reclama) y **un 10 % no se confirma nunca**.
- **Los datos llegan sucios.** Se meten a propósito los errores típicos de los datos reales:
  - pagos repetidos (el sistema los envió dos veces);
  - el mismo valor escrito distinto ("España", " españa", "ESPAÑA");
  - fechas en tres formatos y zonas horarias diferentes;
  - fechas imposibles (31 de febrero);
  - importes en dólares o libras en vez de euros.

### Cómo se limpian

| Paso | Script | Qué hace |
|---|---|---|
| Cargar | `carga_transacciones.py` | Convierte las fechas (que llegan como texto) a un formato único en hora UTC. Las fechas imposibles se apartan a una tabla de "cuarentena" para revisarlas a mano. Comprueba que no se pierde ningún pago. |
| Medir la calidad | `informe_limpieza.py` | Antes de limpiar, cuenta los problemas: nulos, duplicados, formatos, divisas… Lo guarda en `resultados/informe_limpieza.txt`. Que ponga `REVISAR` no es un error: es que ha encontrado los problemas que se metieron a propósito. |
| Limpiar | `limpieza.py` | Quita duplicados, unifica los textos, pasa todo a euros y comprueba que no queda nada pendiente. |

### Las ~35 variables: cómo se describe un pago

Para cada pago se calculan datos que ayudan a ver si es sospechoso (`features.py`). **Solo se usa
información del pasado**: al describir un pago de las 10:00 no se mira nada de después de las
10:00, igual que en la vida real.

| Grupo | Ejemplos | Ayuda a detectar |
|---|---|---|
| Importe | Importe, qué parte del límite de la tarjeta gasta | Bust-out |
| Hora | Hora, día de la semana, si es de madrugada o fin de semana | Fraude nocturno |
| Canal | Online o en tienda, web o app, si se usó autenticación | |
| Sesión y dispositivo | VPN, intentos de contraseña, IP de otro país, móvil nuevo, cuántos clientes usan ese móvil | Robo de cuenta, IP sospechosa, bots |
| Velocidad de la tarjeta | Pagos en los últimos 10 min / 1 h / 24 h, tiempo desde el pago anterior | Card testing, smurfing |
| Velocidad del cliente | Pagos del cliente en la última hora, tarjetas distintas en 24 h | Uso de varias tarjetas |
| Historial del cliente | ¿Este importe es normal para él? (z-score), devoluciones en el último mes | Pico de gasto, devoluciones |

La respuesta (si es fraude o no) **no se usa como variable de entrada**: se guarda aparte. Los
cuatro detectores clásicos ni siquiera la miran; el autoencoder la usa solo para entrenar con pagos
legítimos, y el modelo recalibrado, para aprender de fraudes ya confirmados.

## Los modelos: cinco formas de buscar lo raro

Los cuatro detectores clásicos no usan la etiqueta: buscan **lo que se sale de lo normal** y dan
una nota más alta a los pagos raros. El autoencoder sí usa la etiqueta, pero solo para entrenar con
pagos legítimos. Ninguno recibe "esto es fraude" como variable de entrada. Se probaron los cinco
para ver cuál funciona mejor.

| Modelo | Cómo piensa (explicado fácil) |
|---|---|
| **Isolation Forest** | Va partiendo los pagos con cortes al azar ("¿importe mayor que 50?", "¿de madrugada?"). Un pago raro se queda solo con muy pocos cortes; uno normal necesita muchos. |
| **KMeans** ⭐ | Agrupa los pagos en unos pocos "perfiles típicos" (aquí, 3). La nota es la distancia del pago a su perfil más parecido: si no se parece a ninguno, es raro. |
| **DBSCAN** | Busca las zonas donde hay muchos pagos juntos. Lo que queda lejos de todas esas zonas es raro. |
| **One-Class SVM** | Dibuja una frontera alrededor de la mayoría de los pagos. Lo que cae fuera es raro. |
| **Autoencoder** (red neuronal) | Aprende a "copiar" pagos normales: los comprime y los vuelve a reconstruir. Si un pago no lo sabe reconstruir bien, es que no se parece a lo normal. La nota es lo mal que lo copia. |

**¿Cuál se eligió?** **KMeans**, porque es el que mejor pone el fraude arriba de la lista. Los
cuatro clásicos se probaron con 37 combinaciones de hiperparámetros y se quedó la mejor de cada uno;
el autoencoder se ajustó en una búsqueda aparte (`buscar_autoencoder.py`) y luego se entrenó la
configuración ganadora.

Además de los modelos hay **reglas fijas** (`reglas_alerta.py`), una por tipo de fraude conocido.
Por ejemplo, card testing: *"3 o más pagos de la tarjeta en 10 minutos y el importe es menor de
5 €"*. Son útiles para los fraudes que los modelos no ven bien, pero solo detectan lo que ya se
conoce.

## Los analistas y el aprendizaje semanal

**El problema de KMeans:** busca lo **raro**, no lo que es **fraude**. Por eso, de cada 100 alertas,
solo unas 26 son fraude de verdad.

**La solución:** aprender de lo que ya se sabe. Es lo que en el mundo de la IA se llama
*human-in-the-loop* (una persona dentro del ciclo).

Como no tenemos analistas reales, **se simulan** (`simular_analistas.py`):

- Al final de cada día "revisan" las 6-7 alertas con la nota más alta.
- Dicen si cada una es fraude o legítima. Como es una simulación, miran la respuesta real, pero
  **a veces se equivocan** a propósito: los junior un 8 % de las veces y los senior un 2 %.
- Tardan entre 2 horas y 2 días en responder.
- Todo se guarda en la tabla `feedback_analistas` (qué pago, quién lo revisó, cuándo, qué dijo y
  un comentario).
- Lo que una persona marca en la web (`revision_web`) cuenta igual, y si coincide en el mismo pago,
  manda sobre el analista simulado.

Hay **dos fuentes** para saber qué era fraude, y se juntan (`integrar_feedback.py`):

| Fuente | Ventaja | Inconveniente |
|---|---|---|
| **El banco** (contracargos) | Muy fiable | Tarda unos 12 días, y un 10 % nunca llega |
| **Los analistas** | Rápido (1-2 días) | Solo de las 6-7 alertas revisadas, y a veces se equivocan |

Si las dos fuentes no coinciden, manda el banco.

**Cada semana** (`recalibrar_modelo.py`) se entrena un segundo modelo, ahora sí **supervisado**
(aprende de ejemplos con respuesta). Recibe la nota de KMeans y las ~35 variables, y aprende
*"las alertas con estas características solían ser fraude; estas otras, falsas alarmas"*. A
partir de ese momento las alertas se ordenan con él. KMeans no desaparece: su nota es una de las
cosas que mira el nuevo modelo.

Por último, `medir_feedback.py` comprueba semana a semana si las alertas mejoran.

### Ajustar umbrales y variables con lo validado

Las reglas y el modelo se diseñaron al principio, sin saber cómo funcionarían. Tras **3 semanas
de funcionamiento** (la *fecha de decisión*), el equipo mira lo que ya está validado (lo que
confirmó el banco y lo que dijeron los analistas) y ajusta tres cosas (`ajustar_umbrales.py`):

1. **Los umbrales de las reglas.** Para cada regla se prueban varios valores (por ejemplo, card
   testing con 2, 3, 4… pagos en 10 minutos) y se queda el que mejor equilibra aciertos y falsas
   alarmas. Si una regla casi nunca acierta, se apaga.
2. **Un umbral mínimo para la cola de alertas:** ¿merece la pena revisar las alertas con la nota
   más baja, o casi nunca son fraude?
3. **Las variables del modelo recalibrado:** quitar las que no aportan y añadir variables nuevas
   para el fraude que se escapa.

Tres reglas para no engañarse:

- **Solo se usa lo que se sabía ese día.** Nada de la respuesta real.
- **Un cambio se adopta solo si mejora claramente.** Con pocos fraudes, una mejora pequeña puede ser
  suerte: para quitar variables se exige además que la mejora aguante al repetir la medida con
  remuestreos de los datos (en el 95 % de 200).
- **Después se comprueba** en los días siguientes, que no se usaron para decidir, con la respuesta
  real.

El script no toca la configuración: propone los valores y una persona los copia a `config.yaml`.
Las variables nuevas o quitadas del recalibrador se aplican desde la fecha de decisión; los umbrales
de las reglas se leen de `config.yaml` y afectan a las alertas que se regeneren, también a las
anteriores a esa fecha.

## La automatización

Todo el proceso se ejecuta solo:

| Cuándo | Qué se hace |
|---|---|
| **Cada día** (03:00) | Llegan los pagos nuevos y las confirmaciones del banco → se limpian → se describen → se puntúan → los analistas revisan → si ha pasado una semana, el sistema aprende |
| **Cada semana** (domingo, 05:00) | Se vuelven a entrenar los modelos con el tramo de entrenamiento actualizado por fechas y se revisa el ajuste de umbrales |
| **Una vez, al empezar** | Se crea la base de datos y se cargan los datos iniciales |

Se puede ejecutar de cuatro formas, de más sencilla a más "profesional":

| Forma | Para qué |
|---|---|
| **En tu ordenador** (`python main.py`) | Para desarrollar y probar |
| **Docker** | Para ejecutarlo sin instalar Python |
| **Airflow** | Para que se lance solo cada día, con una web donde ver cada paso y si ha fallado |
| **Kubernetes** | Para ejecutarlo en servidores de forma programada |

En cada ejecución, `validacion_reproducibilidad_consistencia.py` comprueba que todo cuadra (que no
falta ningún pago, que no hay duplicados y que, con la misma semilla, sale exactamente lo mismo).
Si algo falla, la ejecución se marca en rojo.

---

# 3. Resultados

## Cómo sabemos que funciona (y no nos engañamos)

Es muy fácil hacer trampa sin querer y que un modelo parezca mejor de lo que es. Para evitarlo se
mide **como se haría en un banco real**:

1. **El examen es con datos del futuro.** Se descartan los primeros 30 días (calentamiento: no hay
   fraude y aún no hay historial fiable) y el resto se ordena por fecha: el 70 % más antiguo sirve
   para entrenar y el 30 % más reciente es el examen final (test). El modelo nunca ve el examen antes
   de tiempo.
2. **Se elige con unos datos y se mide con otros.** El mejor modelo se elige con la *validación*
   (el final del periodo de entrenamiento) y después se mide en *test*. Si se eligiera mirando el
   test, sería como copiar en el examen.
3. **Las respuestas llegan tarde.** Al elegir, solo se conocen los fraudes que el banco ya había
   confirmado en ese momento, como pasaría de verdad.
4. **Hay un fraude que nunca se ha visto.** El `cash_out_multicomercio` mide si el sistema
   detecta fraude nuevo.
5. **Se da el margen de error.** Con pocos fraudes una cifra puede salir bien por suerte, así que
   los resultados principales llevan su intervalo de confianza.
6. **El resultado se puede repetir.** Con la misma semilla, todo sale exactamente igual.

## Qué ha salido

> Las cifras salen de la ejecución de referencia del 16/10/2026 (la última completa). Pueden
> variar un poco si se vuelve a ejecutar con otros datos o con otra configuración.

### Los modelos

Medido en el test (los últimos 45 días, unos 29.000 pagos y 201 fraudes):

| Modelo | PR-AUC test | Margen (95 %) | Fraude encontrado revisando el 1 % |
|---|---|---|---|
| **KMeans** ⭐ | **0,41** | 0,30 – 0,52 | **42 %** |
| One-Class SVM | 0,37 | 0,26 – 0,48 | 42 % |
| DBSCAN | 0,33 | 0,23 – 0,43 | 32 % |
| Autoencoder | 0,21 | 0,13 – 0,32 | 25 % |
| Isolation Forest | 0,15 | 0,08 – 0,27 | 25 % |

**Cómo leerlo:**

- **Revisando solo el 1 % de los pagos (unos 7 al día), KMeans encuentra el 42 % del fraude.** Si
  se revisara al azar, se encontraría el 1 %.
- **De cada 100 alertas, 26 son fraude de verdad.** Las otras 74 son falsas alarmas. El umbral se
  elige en validación y en test marca un 1,1 % (unas 321 alertas); si se forzara exactamente el
  1 %, la precisión sería del 29 %.
- **Si se revisara el 5 %** (unos 32 al día) se encontraría el 73 % del fraude, pero solo 10 de
  cada 100 alertas serían fraude.
- **Con estos datos no se puede afirmar que KMeans sea mejor que One-Class SVM o DBSCAN.** Sus
  intervalos de confianza se solapan; lo correcto es decir que KMeans "no es peor que los demás",
  no que "es el mejor".

### Qué fraude detecta y cuál no

| Tipo | Lo detecta | Por qué |
|---|---|---|
| Bust-out, dispositivo nuevo | 100 % | Gastan casi todo el límite o usan un móvil nunca visto: muy raro |
| Account takeover | 91 % | Cambio de contraseña + móvil o país raros |
| Bot | 75 % | Clics a velocidad no humana |
| Card testing | 73 % | Muchas compras pequeñísimas seguidas |
| Smurfing | 18 % | Solo es raro en 1 o 2 cosas (importes cerca de 1.000 €) |
| IP sospechosa | 5 % | Solo cambia el país; todo lo demás es normal |
| Pico de gasto, devolución abusiva | 0 % | Solo es raro en 1 cosa (el importe o las devoluciones) |
| **Fraude desconocido** | **0 %** | Imita a un cliente normal: su móvil, su país, importes normales |

**¿Por qué se escapan algunos?** Un detector de anomalías mira **todas las variables a la vez**.
Si un fraude solo es raro en una cosa (por ejemplo, el país) y en todo lo demás es normal, en
conjunto no parece raro. Para estos casos funcionan mejor las **reglas** que miran esa variable
concreta.

**¿Y el fraude desconocido?** Los detectores no supervisados no lo detectan, porque **no es raro**: se
parece a una compra normal. Tampoco lo verán los analistas, porque solo revisan lo que el detector
pone arriba. La única vía es que el banco lo confirme (cuando los clientes reclamen) y que el
modelo recalibrado aprenda a reconocerlo. Con la variable nueva del Hito 6, el modelo recalibrado
sube su presencia en la cola del 9 % al 16 %, pero sigue siendo el tipo que peor se detecta.

### Lo que mejora al aprender (Hito 6)

Comparando **los mismos días con el mismo trabajo** (el 1 % de los pagos: unas 6-7 alertas al día):

| Cómo se ordenan las alertas | Fraudes encontrados | Precisión | Recall |
|---|---|---|---|
| Solo KMeans (sin aprender) | 70 | 29 % | 40 % |
| Aprendiendo de lo que confirma el banco | 98 | 41 % | 56 % |
| Aprendiendo del banco + los analistas | 94 | 39 % | 53 % |

**Cómo leerlo:**

- ✅ **Aprender de los fraudes confirmados mejora mucho.** Con el mismo trabajo, se encuentra un
  40 % más de fraude.
- ⚠️ **Lo que aportan los analistas no se nota en estos datos.** El banco ya confirma unos 500
  fraudes, y unas 6-7 revisiones al día pesan poco al lado. Es un resultado honesto y se cuenta tal
  cual.
- 💡 **Los analistas sí son imprescindibles con los pagos más recientes.** El banco tarda unos
  12 días en confirmar, así que lo de esta semana solo lo saben los analistas.
- 📈 La semana 1 tenía un 26 % de alertas que eran fraude y la semana 6 un 50 %. Pero cuidado: cada
  semana tiene un tipo de fraude distinto, así que la comparación justa es la de la tabla.

### El ajuste de umbrales y variables (Hito 6)

Decidido el 8 de agosto (3 semanas después de empezar) con lo validado hasta ese día, y comprobado
en las semanas siguientes con la respuesta real (`resultados/ajuste_umbrales.md`):

| Qué | Decisión | Resultado en las semanas siguientes |
|---|---|---|
| **Umbrales de las reglas** | Cambian 6 reglas: 5 se hacen más exigentes y se apaga la de devoluciones (acertaba el 1 %) | De 27 a 7 alertas al día; precisión del 10 % al 28 %; recall del 60 % al 48 % |
| **Umbral mínimo de la cola** | No se pone: el mejor umbral solo ahorraría el 10 % de las revisiones, que es el mínimo exigido | — |
| **Quitar variables** | No se quita ninguna: aunque quitar 16 variables mejora la validación (PR-AUC 0,672 → 0,698), la mejora no es estable en los remuestreos (no llega al 95 % exigido) y en validación no hay card testing, así que su importancia está subestimada | — |
| **Variable nueva** `n_comercios_cliente_3h` (comercios distintos del cliente en 3 h) | Se añade: separa muy bien el fraude nuevo que el banco ya había confirmado (AUC 0,96) | 52 fraudes en la cola frente a 52: igual (dentro del ruido). El fraude nuevo pasa del 9 % al 16 % |

**Cómo leerlo:** las reglas mejoran mucho (casi 4 veces menos alertas y casi el triple de
precisión, a cambio de algo de recall). En el modelo recalibrado, el ajuste casi no cambia nada, y
se dice así: con tan pocos fraudes validados no se podía decidir más sin arriesgarse a empeorar.

### Una cosa que parece rara: ¿por qué sale mejor en el examen que en la práctica?

La PR-AUC de KMeans es **0,13 en validación** y **0,41 en test**. Parece que el modelo mejora con
datos nuevos, y eso no tiene sentido. La explicación:

- En validación, al elegir el modelo, solo se conocían **26 de los 57 fraudes** (el resto aún no
  lo había confirmado el banco). Los no confirmados cuentan como pagos normales, así que el modelo
  que los pone arriba parece que se equivoca.
- Si se mide la validación con **todas** las respuestas (solo para comprobarlo, no para elegir),
  sale **0,32**, cerca del 0,41 del test.
- El resto de la diferencia es que la mezcla de tipos no es la misma: en validación pesan más los
  tipos que el detector ve peor (pico de gasto e IP sospechosa: 12 de 57 fraudes, frente a 35 de
  201 en test) y no hay ningún card testing.

## Lo que no hace bien (limitaciones)

Contarlas da credibilidad. Son estas:

- **Los datos son inventados.** Están hechos para ser difíciles, pero las variables se diseñaron
  sabiendo qué tipos de fraude había. En esos tipos, los resultados son algo optimistas. El fraude
  desconocido es la medida más honesta, y ahí el sistema falla casi por completo: 0 % en el detector
  no supervisado y 16 % en la cola tras el ajuste.
- **Hay pocos fraudes para elegir el modelo.** Solo 26 confirmados en validación. Por eso los
  modelos empatan.
- **Casi 3 de cada 4 alertas son falsas alarmas.** Es normal en fraude, pero hay margen de mejora.
- **Los fraudes "de una sola cosa"** (pico de gasto, IP sospechosa, devoluciones) se escapan al
  detector. Necesitan reglas.
- **El presupuesto de revisión de los analistas sigue saliendo solo del modelo.** La web ya enseña
  una cola única (modelo + reglas), pero el 1 % que revisan los analistas se elige con la nota del
  modelo; las reglas solo elevan el nivel en la web. Lo ideal sería que la cola única ordenara
  también ese presupuesto.
- **El ajuste de umbrales se decide con pocos fraudes** (unas decenas validados en 3 semanas). Por
  eso solo se adopta lo que mejora con claridad, y hay que repetirlo cuando haya más datos.
- **Probado en pequeño.** 2.000 clientes y un clúster de Kubernetes de un solo ordenador.

## Preguntas que os van a hacer

**¿Por qué no usar directamente un modelo que aprenda con ejemplos de fraude (supervisado)?**
Porque al empezar no hay ejemplos: el banco tarda semanas en confirmar el fraude, y el fraude
nuevo no tiene ejemplos nunca. Por eso se empieza con un detector de rarezas y, cuando ya hay
fraudes confirmados, se añade el modelo supervisado (Hito 6). Se usan los dos.

**¿Por qué KMeans y no la red neuronal (autoencoder), que es más moderna?**
Porque se eligió el que mejor funciona con los datos, no el más moderno. El autoencoder quedó
cuarto. Además, KMeans es más fácil de explicar: *"este pago no se parece a ninguno de los perfiles
normales"*.

**¿Por qué solo el 1 %?**
Es lo que da tiempo a revisar a un equipo pequeño (unos 7 pagos al día). Se puede cambiar en
`config.yaml`. Revisando el 5 % se encuentra el 73 % del fraude, pero con muchas más falsas alarmas.

**¿Cómo sé que no habéis hecho trampa?**
El examen (test) son los datos más recientes, que nadie toca hasta el final; las variables solo
miran el pasado; y hay tests automáticos que lo comprueban (`tests/test_features_sin_futuro.py`).

**¿Si lo ejecuto yo, me sale lo mismo?**
Sí. Con la misma semilla (`semilla: 42` en `config.yaml`) sale exactamente igual, y cada ejecución
lo comprueba.

**¿Funcionaría con datos reales?**
El pipeline sí, pero habría que adaptar la carga y el esquema de variables al sistema real. Los
resultados habría que volver a medirlos, porque los datos reales tendrán otros patrones.

---

# 4. Usarlo

## Ejecutarlo

### Lo que necesitas

- **Docker Desktop** (para la base de datos ClickHouse).
- **Python 3.13**.

### La primera vez

```powershell
docker compose up -d                                 # arranca la base de datos
python -m venv venv                                  # crea el entorno de Python
venv\Scripts\pip install -r requirements.txt         # instala las librerías
copy .env.example .env                               # y escribe la contraseña de ClickHouse en .env
```

### Comandos del día a día

| Quiero… | Comando | Tarda |
|---|---|---|
| Arrancar la base de datos (si el ordenador se ha reiniciado) | `docker compose up -d` | 1 min |
| **Rehacerlo todo sin borrar nada**: reutiliza los datos (también los días que ha añadido el scoring diario); si la base de datos está vacía, los genera | `venv\Scripts\python.exe main.py` (o el ▶ de VS Code con `main.py` abierto) | ~12 min |
| **Generarlo todo desde cero** (borra la base de datos y vuelve a crear los mismos días que había, idénticos) | `venv\Scripts\python.exe main.py --desde-cero` | ~15 min |
| Desde cero, solo con el dataset original (sin los días del scoring diario) | `venv\Scripts\python.exe main.py --desde-cero --solo-original` | ~15 min |
| Empezar desde un paso concreto | `venv\Scripts\python.exe main.py --desde entrenar_autoencoder` | depende |
| **Simular un día nuevo** (como hace Airflow cada día) | `venv\Scripts\python.exe main.py --scoring` | ~5 min |
| **Abrir la web** (y entrar en http://localhost:8000) | `venv\Scripts\python.exe -m uvicorn web.app:app` | al momento |
| Pasar los tests | `venv\Scripts\python.exe -m pytest tests` | 1 min |

Si un paso falla, el pipeline se para ahí y dice cuál ha sido.

Los días que añade el scoring diario son reproducibles: cada uno usa la semilla del proyecto y su
fecha. Por eso `main.py --desde-cero` puede borrar la base de datos y volver a generarlos idénticos
(`generar_lote.py --dias N` da lo mismo que N lotes de un día). Lo único que borra de verdad lo
acumulado es `--desde-cero --solo-original` o el DAG `fraude_setup`.

### Repetir solo la parte de los analistas

```powershell
# 1. Vaciar las revisiones anteriores
venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); import clickhouse_connect; from config import *; clickhouse_connect.get_client(host=HOST,port=PORT,username=USER,password=PASSWORD,database=DATABASE).command('TRUNCATE TABLE feedback_analistas')"

# 2. Volver a simularlo todo (revisa los ~45 días del test y aprende cada semana)
venv\Scripts\python.exe pipeline\simular_analistas.py
venv\Scripts\python.exe pipeline\integrar_feedback.py
venv\Scripts\python.exe pipeline\recalibrar_modelo.py
venv\Scripts\python.exe pipeline\medir_feedback.py
```

### Con Docker, Airflow o Kubernetes

```powershell
# Docker, sin instalar Python
docker compose run --rm --build pipeline                       # = python main.py (no borra nada)
docker compose run --rm pipeline python main.py --scoring      # un día más
docker compose --profile web up -d --build                     # la web, en http://localhost:8000

# Airflow (después, abrir http://localhost:8080)
docker compose -f docker-compose.yml -f docker-compose.airflow.yml up -d --build
#   1. lanzar fraude_setup (crea los datos)
#   2. lanzar fraude_entrenamiento (entrena)
#   3. activar fraude_scoring (cada día a las 03:00) y fraude_entrenamiento (domingos a las 05:00)

# Kubernetes (clúster local con kind)
docker build -t fraude-pipeline:latest .
kind create cluster --name fraude
kind load docker-image fraude-pipeline:latest --name fraude
kubectl apply -k k8s/
kubectl create -f k8s/job-pipeline-completo.yaml
```

Para Kubernetes hacen falta `kind` y `kubectl`; para Airflow, el compose de Airflow (ya incluido en
el repositorio).

**Importante:** no lanzar a la vez el entrenamiento y el scoring diario, porque los dos rehacen las
mismas tablas. Por eso en Airflow y en Kubernetes tienen horas distintas.

Todos los ajustes (semilla, tamaño de los datos, porcentaje de alertas, errores de los analistas…)
están en **`config.yaml`**, comentados.

## Qué mirar después de ejecutar

| Para ver… | Abre |
|---|---|
| **Los pagos sospechosos y los avisos**, como los vería el cliente | La web: http://localhost:8000 (ver abajo) |
| **El resumen de los modelos**: cuál gana, qué detecta, cuánto fiarse | `resultados/conclusiones.md` |
| Las curvas de los 5 modelos | `resultados/curvas_pr_test.png` |
| La comparación con la red neuronal (Hito 4) | `resultados/conclusiones_hito4.md`, `comparativa_final_pr.png` |
| **Si mejora al aprender** (Hito 6) | `resultados/feedback_semanal.png`, `conclusiones_feedback.md` |
| **El ajuste de umbrales y variables** (Hito 6) | `resultados/ajuste_umbrales.md` (y `ajuste_reglas.csv`, `ajuste_variables.csv`) |
| La calidad de los datos antes de limpiar | `resultados/informe_limpieza.txt` |
| Si la última ejecución cuadra | `resultados/validacion_<fecha>.txt` |
| Los números en tablas | `resultados/comparativa_test.csv`, `recall_por_tipo_test.csv`, `feedback_semanal.csv` |
| Gráficos de exploración | `notebooks/` (necesitan ClickHouse arrancado) |

Y dentro de **ClickHouse** (base de datos `fraude_pagos`):

| Tabla | Qué tiene |
|---|---|
| `transaccion` | Los pagos ya limpios |
| `transacciones_enriquecidas` | Los pagos unidos a cliente, comercio, tarjeta, sesión y dispositivo |
| `features_transaccion` | Las ~35 variables de cada pago |
| `anomaly_scores` | La nota de sospecha de cada pago, con cada modelo |
| `alerta` | Las alertas de las reglas fijas |
| `feedback_analistas` | Lo que dijeron los analistas de cada alerta |
| `riesgo_transaccion` | La nota de cada pago y su puesto en la cola de su día |
| `alertas_unificadas` | (vista) Modelo + reglas: el nivel de alerta de cada pago |
| `aviso` | Los avisos de casos altos y críticos |
| `aviso_leido` | Qué avisos ha marcado como leídos cada comercio |
| `revision_web` | Lo que se marca como fraude o legítima en la web |
| `etiquetas_conocidas` | Lo que se sabe que es fraude (banco + analistas + web) |

Además están las 2 tablas auxiliares de carga (`transaccion_bruta` y `transaccion_cuarentena`) y las
que crea el pipeline.

### La web

La web (`web/`) es la parte que usa el cliente: la entidad que da el servicio de pagos (vista
**Todos los comercios**) o cada comercio (eligiendo el suyo arriba). No enseña modelos ni métricas
técnicas, solo **qué pagos son probablemente fraude, por qué y qué hacer**. Un pequeño servidor
(FastAPI) lee ClickHouse y una página HTML (Chart.js) lo enseña. Se arranca con
`venv\Scripts\python.exe -m uvicorn web.app:app` (o con Docker, ver arriba) y se abre
http://localhost:8000. La hora de la web es la de los datos: "hoy" es el último día puntuado.

**La detección es automática; nadie tiene que revisarlo todo.** Cada día, todos los pagos se
ordenan por la nota del modelo y se les suma lo que dicen las reglas (`generar_avisos.py`, vista
`alertas_unificadas`). Cada alerta trae un nivel, una **acción recomendada** (distinta para la
entidad y para el comercio) y la **probabilidad de fraude** de su nivel, calculada con lo que han
resultado ser los casos ya cerrados de ese nivel:

| Nivel | Cuándo | Acción (comercio) | Acción (entidad) |
|---|---|---|---|
| **Crítico** | Entre el 0,5 % de pagos más sospechosos del día | Retener el pedido | Bloquear la tarjeta |
| **Alto** | Entre el 1 %, o una regla muy segura (nota ≥ 0,8) | Confirmar con el cliente | Revisar hoy |
| *Medio* (desactivado) | Si se activa: entre el 3 %, o cualquier regla | Solo vigilar | Solo vigilar |

El nivel **medio está desactivado** (`alertas.medio: null`): eran ~15 alertas más al día que no
piden ninguna acción y solo cubrían ~6 % del dinero defraudado. En la simulación, crítico y alto,
con unas 7 alertas al día, cubren ~94 % del importe defraudado. Si un cliente lo quiere, se activa
con `medio: 0.03` y la web lo enseña como *en vigilancia*.

**Las alertas se cierran solas.** Mientras está abierta, una alerta está *por resolver*. Se cierra
como **fraude** si llega el contracargo del banco, como **legítima** si alguien la revisa, o **sin
incidencia** si pasan 30 días sin nada de eso (`alertas.dias_cierre`; a los 30 días el banco ya ha
confirmado ~85 % del fraude, y si llega después, la alerta pasa a fraude). Un caso lo deciden sus
pagos más graves: un pago alto sin revisar no deja abierto un caso crítico ya resuelto.

Las alertas de una misma tarjeta en el mismo día forman un **caso** (el card testing o el cash-out
son varios pagos seguidos). Cada caso dice sus **motivos** en lenguaje claro ("importe 21 veces el
habitual del cliente", "conexión desde otro país (Vietnam)", "5 pagos con la misma tarjeta en la
hora anterior"...) y su **puesto en el día** ("top 0,2 % del día"). Los porcentajes se cambian en
`config.yaml` (sección `alertas`).

| Pestaña | Qué enseña |
|---|---|
| **Alertas** | La bandeja de casos por resolver (por defecto altos y críticos), ordenada por probabilidad de fraude, por fecha o por importe. Al abrir uno: qué hacer, la tarjeta, sus pagos de ese día con los motivos y su historial |
| **Resumen** | Casos, importe en riesgo, fraude confirmado, cuánto fraude tuvo alerta (en número de pagos y en dinero) y cuántas alertas acertaron; alertas por día, en qué quedaron y (vista entidad) los comercios con más alertas |
| **Transacciones** | Buscador por ID de pago o de cliente, últimos dígitos de la tarjeta o comercio. Se puede ordenar por fecha, por riesgo o por importe |
| **Avisos** | Un aviso por cada caso alto o crítico nuevo. También en la campana de arriba, que avisa sola si llega uno (se comprueba cada minuto). Los de hace más de 7 días se archivan |

**Revisar es opcional.** La entidad puede dar un veredicto (**Es fraude** / **Es legítimo**). El
comercio no tiene que decidir nada: solo puede aportar lo que sabe (**El cliente reconoce la
compra** / **Confirmo que es fraude**). En su vista solo ve sus propios pagos, sin el nombre del
titular ni sus compras en otros comercios.

**La web también enseña al sistema.** Lo que se marca se guarda en `revision_web` y
`integrar_feedback.py` lo trata como la revisión de un analista (manda sobre la del equipo
simulado; el banco sigue mandando sobre todo). En el siguiente aprendizaje semanal, el modelo ya lo
tiene en cuenta.

**Avisos fuera de la web.** Si se pone una dirección en `alertas.webhook_url` (`config.yaml`), cada
aviso nuevo se envía también allí (Slack, Teams o el sistema del comercio). `avisar_desde` decide
desde qué nivel se avisa. Para reescribir los avisos tras cambiar la configuración:
`venv\Scripts\python.exe pipeline\generar_avisos.py --rehacer`.

Abajo a la derecha está el **modo demostración**: enseña qué era fraude de verdad según el
simulador, para comprobar en una presentación que las alertas aciertan. Los análisis técnicos
(modelos, curvas, ajuste de umbrales) y los patrones de fraude están en `resultados/` y `notebooks/`.

La documentación de los datos que da el servidor está en http://localhost:8000/api/docs.

## Mapa del proyecto

### Carpetas y archivos

| Ruta | Qué hay |
|---|---|
| `pipeline/` | Los scripts del pipeline (uno por paso) |
| `main.py` | El que lanza los pasos del pipeline en orden |
| `config.yaml` | Todos los ajustes |
| `notebooks/` | Exploración de los datos y gráficos (Hitos 1 y 2) |
| `dags/` | Los flujos de Airflow |
| `k8s/` | La configuración de Kubernetes |
| `web/` | La web para el cliente (servidor en `app.py`, página en `static/`) |
| `tests/` | Comprobaciones automáticas |
| `datos_ml/` | Los datos ya preparados para los modelos (se genera) |
| `modelos/` | Los modelos entrenados (se genera) |
| `resultados/` | Informes, tablas y gráficos (se genera) |
| `historico/` | Copias de las últimas ejecuciones para poder volver atrás (5 de clásicos y autoencoder; todas las recalibraciones) |
| `experimentos/` | Pruebas aparte que no lanza `main.py`, como probar el autoencoder con un dataset nuevo: `python experimentos/generar_test_autoencoder.py` y después `python experimentos/evaluar_test_autoencoder.py` |
| `legacy/` | Código antiguo que ya no se usa |

### Los pasos, en orden

| # | Script | Qué hace |
|---|---|---|
| 1 | `crear_tablas.py` | Crea la base de datos y las tablas vacías |
| 2 | `dataset.py` | Inventa los datos (clientes, pagos, fraudes) con errores a propósito |
| 3 | `carga_transacciones.py` | Carga los pagos y arregla las fechas |
| 4 | `informe_limpieza.py` | Cuenta los problemas de calidad |
| 5 | `limpieza.py` | Arregla los problemas |
| 6 | `crear_tabla_enriquecida.py` | Junta en una tabla todo lo de cada pago (cliente, comercio, sesión…) |
| 7 | `features.py` | Calcula las ~35 variables |
| 8 | `reglas_alerta.py` | Aplica las reglas fijas |
| 9 | `preparacion_datos_ml.py` | Parte los datos por fechas (entrenamiento / validación / test) y los prepara |
| 10 | `modelos_nosupervisados.py` | Entrena Isolation Forest, KMeans, DBSCAN y One-Class SVM |
| 11 | `entrenar_autoencoder.py` | Entrena la red neuronal |
| 12-13 | `puntuar_nosupervisados.py`, `puntuar_autoencoder.py` | Ponen nota a los pagos que aún no la tienen |
| 14 | `simular_analistas.py` | Los analistas revisan las alertas de cada día |
| 15 | `generar_avisos.py` | Une modelo y reglas en una cola con niveles (crítico y alto; medio opcional) y crea los avisos |
| 16 | `integrar_feedback.py` | Junta lo que dicen los analistas (y la web) con lo que confirma el banco |
| 17 | `recalibrar_modelo.py` | Cada semana, el sistema aprende de lo confirmado |
| 18 | `medir_feedback.py` | Mide si las alertas mejoran semana a semana |
| 19 | `ajustar_umbrales.py` | Propone umbrales de reglas, umbral de la cola y variables con lo validado |
| 20-22 | `evaluar_modelos_nosupervisados.py`, `evaluar_autoencoders.py`, `comparacion_modelos.py` | Comparan los modelos y escriben las conclusiones |
| 23 | `validacion_reproducibilidad_consistencia.py` | Comprueba que todo cuadra |

El scoring diario (`main.py --scoring`) hace: generar un día nuevo (`generar_lote.py`, que trae
también las confirmaciones del banco de ese día) → pasos 3, 5-8, 12-18 y 23. Nunca vuelve a
entrenar los modelos principales ni a ajustar umbrales (eso es una decisión periódica).

## Correspondencia con los hitos

| Hito | Qué se pedía | Dónde está | Estado |
|---|---|---|---|
| 1. Introducción y exploración | Base de datos, carga de datos, informe de limpieza, primeros gráficos | `crear_tablas.py`, `dataset.py`, `carga_transacciones.py`, `informe_limpieza.py`, `notebooks/exploracion_visualizacion.ipynb` | Hecho |
| 2. Estadística y preprocesamiento | Limpieza, valores atípicos (Z-score, IQR), patrones de fraude, dataset limpio | `limpieza.py`, `notebooks/outliers_patrones_tecnicas.ipynb`, `preparacion_datos_ml.py` | Hecho |
| 3. ML para anomalías | Isolation Forest, One-Class SVM, KMeans, DBSCAN; variables; ajuste; puntuación | `features.py`, `modelos_nosupervisados.py`, `puntuar_nosupervisados.py`, `evaluar_modelos_nosupervisados.py` | Hecho |
| 4. Autoencoders | Red neuronal, error de reconstrucción, comparación con los clásicos | `entrenar_autoencoder.py`, `puntuar_autoencoder.py`, `evaluar_autoencoders.py`, `comparacion_modelos.py` | Hecho |
| 5. Orquestación y despliegue | Airflow, Docker, Kubernetes, ejecuciones periódicas, reproducibilidad | `dags/`, `Dockerfile`, `docker-compose*.yml`, `k8s/`, `generar_lote.py`, `validacion_reproducibilidad_consistencia.py` | Hecho |
| 6. Analistas y dashboards | Feedback de analistas, ajuste de umbrales, dashboards, patrones por cliente/comercio/dispositivo/hora | Feedback: `simular_analistas.py`, `integrar_feedback.py`, `recalibrar_modelo.py`, `medir_feedback.py`. Umbrales y variables: `ajustar_umbrales.py`, `config.yaml` (`reglas`, `feedback`). Dashboard: la web (`web/`) con alertas por niveles, avisos y revisión. Patrones: `notebooks/` y `resultados/` | Hecho |
