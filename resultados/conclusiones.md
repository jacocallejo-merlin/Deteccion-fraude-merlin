# Conclusiones: detección de anomalías con modelos no supervisados

- El mejor detector es KMeans.
- Si el equipo revisa el 1 % de las transacciones que el modelo ve más sospechosas, encuentra el 42 % del fraude, y 26 de cada 100 revisiones son fraude real.
- Detecta bien account_takeover, bust_out, bot, dispositivo_nuevo y card_testing.
- Se le escapan casi por completo cash_out_multicomercio, smurfing, ip_sospechosa, pico_gasto y devolucion_abusiva.

## Cómo se ha elegido
- Se probaron 5 modelos de detección de anomalías, cada uno con varias configuraciones distintas (Isolation Forest 8, KMeans 5, One-Class SVM 4 y DBSCAN 20). De cada modelo se quedó la mejor y luego se compararon los 5.
- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.
- Excepción: el autoencoder es semi-supervisado (se entrena solo con transacciones legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). La comparación con los demás no es del todo en igualdad de condiciones.
- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo más reciente (test), que no se usó para nada antes. Así la medida es honesta.
- Como en un banco, al elegir solo se conocían los fraudes ya confirmados: en validación había 56 fraudes, pero solo 23 estaban confirmados al final del periodo de entrenamiento (el resto se confirma más tarde o nunca). Para elegir, los no confirmados cuentan como legítimos. El test se mide con todos.

## El mejor modelo y por qué
- KMeans agrupa las transacciones en perfiles típicos (clusters) y marca como sospechosas las que quedan lejos de todos los perfiles.
- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar sacaría 0.007 (la proporción de fraude). KMeans saca 0.41, unas 60 veces más que el azar.
- Cada modelo por separado: KMeans 0.41, One-Class SVM 0.36, DBSCAN 0.33, Autoencoder 0.24, Isolation Forest 0.15.
- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC KMeans 0.08, One-Class SVM 0.08, DBSCAN 0.08, Autoencoder 0.07 y Isolation Forest 0.04).
- Revisando el 1 % de las transacciones del test, fraudes encontrados de 198: Isolation Forest 51, KMeans 83, DBSCAN 64, One-Class SVM 83 y Autoencoder 63.
- El test lo confirma: KMeans es el que más fraude encuentra.
- Ojo: en validación empata con One-Class SVM y DBSCAN. No se puede asegurar que sea mejor, solo que no es peor.

## Qué fraudes detecta y cuáles no
- cash_out_multicomercio (45 casos: fraude NUEVO que no aparece en entrenamiento: credenciales robadas usadas desde el propio dispositivo del cliente para comprar en varios comercios seguidos): detecta el 0 %.
- smurfing (22 casos: una cantidad grande repartida en muchas pequeñas para no llamar la atención): detecta el 18 %.
- account_takeover (21 casos: alguien roba el acceso a una cuenta y opera con ella): detecta el 90 %.
- bust_out (19 casos: el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito): detecta el 100 %.
- bot (19 casos: transacciones automáticas, muy rápidas y repetitivas): detecta el 74 %.
- ip_sospechosa (18 casos: operaciones desde una IP o ubicación sospechosa): detecta el 6 %.
- dispositivo_nuevo (18 casos: operaciones desde un dispositivo que el cliente no había usado nunca): detecta el 100 %.
- pico_gasto (16 casos: un gasto mucho mayor de lo habitual para ese cliente): detecta el 0 %.
- card_testing (11 casos: muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona): detecta el 73 %.
- devolucion_abusiva (9 casos: devoluciones o contracargos fraudulentos): detecta el 0 %.
- cash_out_multicomercio es el fraude nuevo (sin casos en entrenamiento ni variables pensadas para él) y solo se detecta el 0 %. Imita el comportamiento normal del cliente: su dispositivo, su país e importes moderados. Un detector de anomalías solo encuentra lo que se sale de lo normal, así que el fraude que se parece a una compra legítima necesita otra vía: que los analistas lo marquen al revisarlo y se añadan variables o reglas nuevas (hito 6).
- Los que se escapan no es por falta de variables: el modelo ya tiene variables que los distinguen. El problema es que los detectores de anomalías puntúan lo rara que es la transacción en conjunto, mirando todas las variables a la vez. Un fraude que solo se sale de lo normal en una o dos variables (por ejemplo, una IP de otro país con todo lo demás normal) no queda en el 1 % más raro. Variables que ya los recogen:
  - smurfing: n_tarjeta_1h, n_tarjeta_24h, importe_tarjeta_10min, n_cliente_1h.
  - ip_sospechosa: ip_extranjera, proxy_vpn.
  - pico_gasto: z_importe_cliente, ratio_importe_habitual.
  - devolucion_abusiva: n_devoluciones_30d.
- Para estos casos funcionan mejor reglas sobre esas variables concretas (reglas_alerta.py) que un detector de anomalías global.

## ¿Cuánto podemos fiarnos?
- Con los datos disponibles, la PR-AUC real estaría entre 0.30 y 0.51 (intervalo de confianza del 95 %).
- En test sube respecto a validación (0.08 a 0.41) sobre todo por las etiquetas: en validación los fraudes aún no confirmados cuentan como legítimos, así que un modelo que los pone arriba parece equivocarse. En test se mide con todas las etiquetas. La cifra de referencia es la de test, con su intervalo de confianza; la de validación solo sirve para comparar modelos entre sí.
- Los resultados no dependen del azar interno de los modelos: con distintas semillas salen prácticamente iguales.
- Solo hay 23 fraudes en validación, así que las decisiones tienen bastante margen de error.
- Revisar el 1 % supone unas 7.0 alertas al día.
- Revisando el 5 % se llegaría al 72 % del fraude, pero solo 10 de cada 100 revisiones serían fraude.
- El umbral fijado en validación marcó el 1.07 % en test, cerca de lo previsto: se puede usar tal cual.
