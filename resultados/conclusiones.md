# Conclusiones: detección de anomalías con modelos no supervisados

- El mejor detector es KMeans.
- Si el equipo revisa el 1 % de las transacciones que el modelo ve más sospechosas, encuentra el 48 % del fraude, y 82 de cada 100 revisiones son fraude real.
- Detecta bien card_testing, account_takeover y bust_out.
- Se le escapan casi por completo ip_sospechosa, pico_gasto, devolucion_abusiva y smurfing.

## Cómo se ha elegido
- Se probaron 4 modelos de detección de anomalías, cada uno con muchas configuraciones distintas (Isolation Forest 8, KMeans 5 y One-Class SVM 4). De cada modelo se quedó la mejor y luego se compararon los 4.
- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.
- Excepción: el autoencoder es semi-supervisado (se entrena solo con transacciones legítimas, así que usa la etiqueta para elegir sus datos de entrenamiento). La comparación con los demás no es del todo en igualdad de condiciones.
- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo más reciente (test), que no se usó para nada antes. Así la medida es honesta.

## El mejor modelo y por qué
- KMeans agrupa las transacciones en perfiles típicos (clusters) y marca como sospechosas las que quedan lejos de todos los perfiles.
- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar sacaría 0.022 (la proporción de fraude). KMeans saca 0.65, unas 29 veces más que el azar.
- Cada modelo por separado: KMeans 0.65, Autoencoder 0.58, One-Class SVM 0.55, Isolation Forest 0.45.
- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC KMeans 0.44, Autoencoder 0.36, One-Class SVM 0.32 y Isolation Forest 0.31).
- Revisando el 1 % de las transacciones del test, fraudes encontrados de 222: Isolation Forest 60, KMeans 106, One-Class SVM 89 y Autoencoder 104.
- El test lo confirma: KMeans es el que más fraude encuentra.
- Donde más destaca sobre los otros es en bot.

## Qué fraudes detecta y cuáles no
- card_testing (32 casos: muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona): detecta el 88 %.
- dispositivo_nuevo (28 casos: operaciones desde un dispositivo que el cliente no había usado nunca): detecta el 57 %.
- account_takeover (27 casos: alguien roba el acceso a una cuenta y opera con ella): detecta el 93 %.
- ip_sospechosa (25 casos: operaciones desde una IP o ubicación sospechosa): detecta el 0 %.
- bot (25 casos: transacciones automáticas, muy rápidas y repetitivas): detecta el 52 %.
- pico_gasto (23 casos: un gasto mucho mayor de lo habitual para ese cliente): detecta el 4 %.
- devolucion_abusiva (22 casos: devoluciones o contracargos fraudulentos): detecta el 0 %.
- smurfing (22 casos: una cantidad grande repartida en muchas pequeñas para no llamar la atención): detecta el 23 %.
- bust_out (18 casos: el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito): detecta el 100 %.
- Los que se escapan no se arreglan cambiando de modelo: las variables actuales no recogen lo que los hace distintos. Habría que añadir:
  - ip_sospechosa: variables de IP: país, si es nueva para el cliente, cuántos clientes la usan.
  - pico_gasto: el importe comparado con la media del propio cliente (z-score por cliente).
  - devolucion_abusiva: historial de devoluciones del cliente.
  - smurfing: número e importe acumulado de transacciones en las últimas horas.

## ¿Cuánto podemos fiarnos?
- Con los datos disponibles, la PR-AUC real estaría entre 0.54 y 0.74 (intervalo de confianza del 95 %).
- Da resultados parecidos en validación y test (0.44 y 0.65): funciona igual con datos nuevos.
- Los resultados no dependen del azar interno de los modelos: con distintas semillas salen prácticamente iguales.
- Las etiquetas no son perfectas: algunas "falsas alarmas" podrían ser fraudes que nadie confirmó, así que la precisión real puede ser algo mayor.
- Revisar el 1 % supone unas 2.8 alertas al día.
- Revisando el 5 % se llegaría al 76 % del fraude, pero solo 34 de cada 100 revisiones serían fraude.
- El umbral fijado en validación marcó el 1.30 % en test, cerca de lo previsto: se puede usar tal cual.
