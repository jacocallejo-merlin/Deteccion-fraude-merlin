"""
¿Cuánto fraude detecta el sistema entero? Por tipo de fraude, en los días del examen (desde el inicio del
test), con la etiqueta real (como el test: el sistema no la ve, solo se usa para medir).

Para cada tipo dice qué parte de sus pagos:
  - cola:    llegó a la cola de los analistas (lo que revisan cada día, feedback.presupuesto);
  - alerta:  tuvo alerta de nivel alto o crítico (lo que ve la web y avisa: modelo o regla, ver
             generar_avisos.py, vista alertas_unificadas);
  - regla:   saltó alguna regla (reglas_alerta.py).
Y, semana a semana, la parte de cada tipo con alerta: así se ve si el recalibrado semanal "olvida"
algún tipo con el tiempo (lo que le pasaba al card testing).

    python pipeline/medir_sistema.py                 # tabla por pantalla y resultados/sistema_por_tipo.csv
    python pipeline/medir_sistema.py --etiqueta antes  # guarda también una copia con ese nombre, para comparar
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import CARPETA_RESULTADOS
from recalibrar_modelo import conectar, inicio_test

SQL = '''
SELECT t.transaccion_id AS transaccion_id, t.tipo_fraude AS tipo, toDate(t.timestamp) AS dia,
       toFloat64(t.cantidad) AS importe,
       ifNull(r.en_cola, 0) AS cola,
       a.transaccion_id != 0 AS alerta,
       g.transaccion_id != 0 AS regla
FROM transaccion AS t
INNER JOIN (SELECT transaccion_id, en_cola FROM riesgo_transaccion FINAL) AS r ON r.transaccion_id = t.transaccion_id
LEFT JOIN (SELECT transaccion_id FROM alertas_unificadas WHERE prioridad >= 2) AS a ON a.transaccion_id = t.transaccion_id
LEFT JOIN (SELECT DISTINCT transaccion_id FROM alerta WHERE origen = 'regla') AS g ON g.transaccion_id = t.transaccion_id
WHERE t.es_fraude = 1 AND toDate(t.timestamp) >= {inicio:Date}
'''


def medir(client):
    df = client.query_df(SQL, parameters={'inicio': inicio_test()})
    if df.empty:
        raise SystemExit('No hay fraude puntuado en el examen: ejecuta antes el pipeline (simular_analistas.py)')
    por_tipo = df.groupby('tipo').agg(casos=('transaccion_id', 'size'), cola=('cola', 'mean'),
                                      alerta=('alerta', 'mean'), regla=('regla', 'mean'))
    total = pd.DataFrame([{'casos': len(df), 'cola': df['cola'].mean(), 'alerta': df['alerta'].mean(),
                           'regla': df['regla'].mean()}], index=['TODOS'])
    dinero = df.loc[df['alerta'].astype(bool), 'importe'].sum() / df['importe'].sum()
    df['semana'] = (pd.to_datetime(df['dia']) - pd.Timestamp(inicio_test())).dt.days // 7 + 1
    semanal = df.pivot_table(index='tipo', columns='semana', values='alerta', aggfunc='mean')
    return pd.concat([por_tipo.sort_values('alerta'), total]), semanal, dinero


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0].strip())
    p.add_argument('--etiqueta', help='guarda también resultados/sistema_por_tipo_<etiqueta>.csv')
    args = p.parse_args()
    tabla, semanal, dinero = medir(conectar())
    pct = lambda x: f'{x:6.1%}'
    print(f'Fraude del examen detectado por el sistema (desde {inicio_test():%Y-%m-%d})\n')
    print(tabla.to_string(formatters={'cola': pct, 'alerta': pct, 'regla': pct}))
    print(f'\nDinero defraudado con alerta: {dinero:.1%}')
    print('\nParte de cada tipo con alerta, semana a semana:\n')
    print(semanal.to_string(float_format=lambda x: f'{x:5.0%}', na_rep='    -'))
    tabla.to_csv(CARPETA_RESULTADOS / 'sistema_por_tipo.csv')
    if args.etiqueta:
        tabla.to_csv(CARPETA_RESULTADOS / f'sistema_por_tipo_{args.etiqueta}.csv')


if __name__ == '__main__':
    main()
