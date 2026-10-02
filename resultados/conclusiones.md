# Conclusiones: detección de anomalías con modelos no supervisados

- El mejor detector es KMeans.
- Si el equipo revisa el 1 % de las transacciones que el modelo ve más sospechosas, encuentra el 35 % del fraude, y 65 de cada 100 revisiones son fraude real.
- Detecta bien account_takeover, bust_out y card_testing.
- Se le escapan casi por completo dispositivo_nuevo, devolucion_abusiva, pico_gasto, smurfing, ip_sospechosa y bot.

## Cómo se ha elegido
- Se probaron 3 modelos de detección de anomalías, cada uno con muchas configuraciones distintas (Isolation Forest 18, KMeans 16 y One-Class SVM 15). De cada modelo se quedó la mejor y luego se compararon los 3.
- Los modelos aprenden sin etiquetas: solo ven cómo son las transacciones y buscan las raras. Las etiquetas de fraude solo se usan para elegir el mejor y medirlo.
- El mejor se eligió con un periodo de datos (validación) y se comprobó en el periodo más reciente (test), que no se usó para nada antes. Así la medida es honesta.

## El mejor modelo y por qué
- KMeans agrupa las transacciones en perfiles típicos (clusters) y marca como sospechosas las que quedan lejos de todos los perfiles.
- Para comparar se usa la PR-AUC: si se ordenan las transacciones de más a menos sospechosa, mide cuánto fraude queda arriba. Va de 0 a 1, y un modelo al azar sacaría 0.012 (la proporción de fraude). KMeans saca 0.50, unas 43 veces más que el azar.
- Cada modelo por separado: KMeans 0.50, Isolation Forest 0.35, One-Class SVM 0.24.
- Se eligió porque es el que mejor separa el fraude en validación (PR-AUC KMeans 0.66, One-Class SVM 0.54 y Isolation Forest 0.49).
- Revisando el 1 % de las transacciones del test, fraudes encontrados de 137: Isolation Forest 35, KMeans 48 y One-Class SVM 36.
- El test lo confirma: KMeans es el que más fraude encuentra.
- Donde más destaca sobre los otros es en account_takeover y bust_out.
- One-Class SVM queda el último: se entrena con una muestra de 10.000 transacciones (es muy lento con más) y es muy sensible a sus parámetros.

## Qué fraudes detecta y cuáles no
- dispositivo_nuevo (58 casos: operaciones desde un dispositivo que el cliente no había usado nunca): detecta el 2 %.
- account_takeover (33 casos: alguien roba el acceso a una cuenta y opera con ella): detecta el 91 %.
- devolucion_abusiva (15 casos: devoluciones o contracargos fraudulentos): detecta el 13 %.
- bust_out (11 casos: el cliente se comporta con normalidad un tiempo y de golpe agota todo el crédito): detecta el 100 %.
- pico_gasto (5 casos: un gasto mucho mayor de lo habitual para ese cliente): detecta el 0 %.
- smurfing (5 casos: una cantidad grande repartida en muchas pequeñas para no llamar la atención): detecta el 20 %.
- card_testing (5 casos: muchas compras pequeñas seguidas para comprobar si una tarjeta robada funciona): detecta el 60 %.
- ip_sospechosa (4 casos: operaciones desde una IP o ubicación sospechosa): detecta el 0 %.
- bot (1 casos: transacciones automáticas, muy rápidas y repetitivas): detecta el 0 %.
- Los que se escapan no se arreglan cambiando de modelo: las variables actuales no recogen lo que los hace distintos. Habría que añadir:
  - dispositivo_nuevo: si el dispositivo es nuevo para ese cliente.
  - devolucion_abusiva: historial de devoluciones del cliente.
  - pico_gasto: el importe comparado con la media del propio cliente (z-score por cliente).
  - smurfing: número e importe acumulado de transacciones en las últimas horas.
  - ip_sospechosa: variables de IP: país, si es nueva para el cliente, cuántos clientes la usan.
  - bot: tiempo desde la transacción anterior y número de transacciones por minuto.

## ¿Cuánto podemos fiarnos?
- Con los datos disponibles, la PR-AUC real estaría entre 0.39 y 0.62 (intervalo de confianza del 95 %).
- En test baja respecto a validación (0.66 a 0.50). Es en parte normal (al elegir el mejor de varios, su nota de validación sale algo inflada) y en parte las transacciones recientes son algo distintas.
- Los resultados no dependen del azar interno de los modelos: con distintas semillas salen prácticamente iguales.
- Las etiquetas no son perfectas: algunas "falsas alarmas" podrían ser fraudes que nadie confirmó, así que la precisión real puede ser algo mayor.

## En el día a día
- Revisar el 1 % supone unas 1.3 alertas al día.
- Revisando el 5 % se llegaría al 85 % del fraude, pero solo 20 de cada 100 revisiones serían fraude.
- En el periodo de test el umbral marcó el 0.62 % en lugar del 1 % previsto: las transacciones recientes son algo distintas y el umbral habría que reajustarlo cada cierto tiempo.
