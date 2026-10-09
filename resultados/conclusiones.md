# Conclusiones: detección de anomalías con modelos no supervisados

- El mejor detector es Autoencoder.
- Si el equipo revisa el 1 % de las transacciones que el modelo ve más sospechosas, encuentra el 39 % del fraude, y 77 de cada 100 revisiones son fraude real.
- Detecta bien dispositivo_nuevo, account_takeover y bust_out.
- Se le escapan casi por completo devolucion_abusiva, ip_sospechosa, pico_gasto y smurfing.

## Cómo se ha elegido
- Se probaron 5 modelos de detección de anomalías, cada uno con varias configuraciones distintas (Isolation Forest 8, KMeans 5, One-Class SVM 4 y DBSCAN 20). De cada modelo se quedó la mejor y luego se compararon los 5.
- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.
- Excepción: el autoencoder es semi-supervisado (se entrena solo con transacciones legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). La comparación con los demás no es del todo en igualdad de condiciones.
- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo más reciente (test), que no se usó para nada antes. Así la medida es honesta.

## El mejor modelo y por qué
- Autoencoder es una red neuronal que aprende a comprimir y reconstruir las transacciones normales; las que reconstruye mal son las más raras.
- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar sacaría 0.022 (la proporción de fraude). Autoencoder saca 0.55, unas 26 veces más que el azar.
- Cada modelo por separado: KMeans 0.58, Autoencoder 0.55, DBSCAN 0.55, One-Class SVM 0.49, Isolation Forest 0.36.
- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC Autoencoder 0.45, KMeans 0.42, DBSCAN 0.40, One-Class SVM 0.33 y Isolation Forest 0.14).
- Revisando el 1 % de las transacciones del test, fraudes encontrados de 217: Isolation Forest 59, KMeans 91, DBSCAN 98, One-Class SVM 80 y Autoencoder 85.
- En test, DBSCAN encuentra algo más (98 frente a 85). No cambia la elección: el modelo se elige con validación, y elegirlo mirando el test sería hacer trampa.
- Ojo: en validación empata con KMeans. No se puede asegurar que sea mejor, solo que no es peor.

## Qué fraudes detecta y cuáles no
- devolucion_abusiva (30 casos: devoluciones o contracargos fraudulentos): detecta el 3 %.
- bot (28 casos: transacciones automáticas, muy rápidas y repetitivas): detecta el 46 %.
- ip_sospechosa (26 casos: operaciones desde una IP o ubicación sospechosa): detecta el 0 %.
- pico_gasto (26 casos: un gasto mucho mayor de lo habitual para ese cliente): detecta el 0 %.
- dispositivo_nuevo (26 casos: operaciones desde un dispositivo que el cliente no había usado nunca): detecta el 88 %.
- account_takeover (25 casos: alguien roba el acceso a una cuenta y opera con ella): detecta el 68 %.
- bust_out (22 casos: el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito): detecta el 100 %.
- smurfing (17 casos: una cantidad grande repartida en muchas pequeñas para no llamar la atención): detecta el 0 %.
- card_testing (17 casos: muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona): detecta el 53 %.
- Los que se escapan no es por falta de variables: el modelo ya tiene variables que los distinguen. El problema es que los detectores de anomalías puntúan lo rara que es la transacción en conjunto, mirando todas las variables a la vez. Un fraude que solo se sale de lo normal en una o dos variables (por ejemplo, una IP de otro país con todo lo demás normal) no queda en el 1 % más raro. Variables que ya los recogen:
  - devolucion_abusiva: n_devoluciones_30d.
  - ip_sospechosa: ip_extranjera, proxy_vpn.
  - pico_gasto: z_importe_cliente, ratio_importe_habitual.
  - smurfing: n_tarjeta_1h, n_tarjeta_24h, importe_tarjeta_10min, n_cliente_1h.
- Para estos casos funcionan mejor reglas sobre esas variables concretas (reglas_alerta.py) que un detector de anomalías global.

## ¿Cuánto podemos fiarnos?
- Con los datos disponibles, la PR-AUC real estaría entre 0.47 y 0.63 (intervalo de confianza del 95 %).
- En test sube respecto a validación (0.45 a 0.55). No es que el modelo mejore con datos nuevos: validación solo tiene 60 fraudes y la PR-AUC depende mucho de qué tipos de fraude caigan en cada periodo (unos se detectan mucho mejor que otros). La cifra de referencia es la de test, con su intervalo de confianza.
- Los resultados no dependen del azar interno de los modelos: con distintas semillas salen prácticamente iguales.
- Revisar el 1 % supone unas 2.4 alertas al día.
- Revisando el 5 % se llegaría al 69 % del fraude, pero solo 30 de cada 100 revisiones serían fraude.
- El umbral fijado en validación marcó el 1.10 % en test, cerca de lo previsto: se puede usar tal cual.
