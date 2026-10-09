# Conclusiones: detección de anomalías con modelos no supervisados

- El mejor detector es KMeans.
- Si el equipo revisa el 1 % de las transacciones que el modelo ve más sospechosas, encuentra el 54 % del fraude, y 80 de cada 100 revisiones son fraude real.
- Detecta bien account_takeover, bust_out, dispositivo_nuevo y card_testing.
- Se le escapan casi por completo ip_sospechosa, pico_gasto, smurfing y devolucion_abusiva.

## Cómo se ha elegido
- Se probaron 5 modelos de detección de anomalías, cada uno con varias configuraciones distintas (Isolation Forest 8, KMeans 5, One-Class SVM 4 y DBSCAN 20). De cada modelo se quedó la mejor y luego se compararon los 5.
- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.
- Excepción: el autoencoder es semi-supervisado (se entrena solo con transacciones legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). La comparación con los demás no es del todo en igualdad de condiciones.
- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo más reciente (test), que no se usó para nada antes. Así la medida es honesta.

## El mejor modelo y por qué
- KMeans agrupa las transacciones en perfiles típicos (clusters) y marca como sospechosas las que quedan lejos de todos los perfiles.
- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar sacaría 0.018 (la proporción de fraude). KMeans saca 0.63, unas 35 veces más que el azar.
- Cada modelo por separado: KMeans 0.63, DBSCAN 0.59, Autoencoder 0.56, One-Class SVM 0.56, Isolation Forest 0.38.
- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC KMeans 0.55, DBSCAN 0.49, Autoencoder 0.47, One-Class SVM 0.46 y Isolation Forest 0.30).
- Revisando el 1 % de las transacciones del test, fraudes encontrados de 182: Isolation Forest 50, KMeans 98, DBSCAN 87, One-Class SVM 81 y Autoencoder 83.
- El test lo confirma: KMeans es el que más fraude encuentra.
- Donde más destaca sobre los otros es en bot.

## Qué fraudes detecta y cuáles no
- account_takeover (27 casos: alguien roba el acceso a una cuenta y opera con ella): detecta el 100 %.
- bust_out (26 casos: el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito): detecta el 100 %.
- ip_sospechosa (22 casos: operaciones desde una IP o ubicación sospechosa): detecta el 0 %.
- bot (22 casos: transacciones automáticas, muy rápidas y repetitivas): detecta el 54 %.
- dispositivo_nuevo (21 casos: operaciones desde un dispositivo que el cliente no había usado nunca): detecta el 86 %.
- pico_gasto (19 casos: un gasto mucho mayor de lo habitual para ese cliente): detecta el 0 %.
- card_testing (17 casos: muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona): detecta el 88 %.
- smurfing (14 casos: una cantidad grande repartida en muchas pequeñas para no llamar la atención): detecta el 0 %.
- devolucion_abusiva (14 casos: devoluciones o contracargos fraudulentos): detecta el 0 %.
- Los que se escapan no es por falta de variables: el modelo ya tiene variables que los distinguen. El problema es que los detectores de anomalías puntúan lo rara que es la transacción en conjunto, mirando todas las variables a la vez. Un fraude que solo se sale de lo normal en una o dos variables (por ejemplo, una IP de otro país con todo lo demás normal) no queda en el 1 % más raro. Variables que ya los recogen:
  - ip_sospechosa: ip_extranjera, proxy_vpn.
  - pico_gasto: z_importe_cliente, ratio_importe_habitual.
  - smurfing: n_tarjeta_1h, n_tarjeta_24h, importe_tarjeta_10min, n_cliente_1h.
  - devolucion_abusiva: n_devoluciones_30d.
- Para estos casos funcionan mejor reglas sobre esas variables concretas (reglas_alerta.py) que un detector de anomalías global.

## ¿Cuánto podemos fiarnos?
- Con los datos disponibles, la PR-AUC real estaría entre 0.53 y 0.72 (intervalo de confianza del 95 %).
- En test sube respecto a validación (0.55 a 0.63). No es que el modelo mejore con datos nuevos: validación solo tiene 81 fraudes y la PR-AUC depende mucho de qué tipos de fraude caigan en cada periodo (unos se detectan mucho mejor que otros). La cifra de referencia es la de test, con su intervalo de confianza.
- Los resultados no dependen del azar interno de los modelos: con distintas semillas salen prácticamente iguales.
- Revisar el 1 % supone unas 2.7 alertas al día.
- Revisando el 5 % se llegaría al 79 % del fraude, pero solo 29 de cada 100 revisiones serían fraude.
- El umbral fijado en validación marcó el 1.21 % en test, cerca de lo previsto: se puede usar tal cual.
