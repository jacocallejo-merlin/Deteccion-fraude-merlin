"""
Junta las dos fuentes de etiquetas que tendría un banco en un momento dado (Hito 6):
  - banco:    fraudes confirmados (contracargo, reclamación del cliente), con días o semanas de retraso
  - analista: veredictos de feedback_analistas, que llegan en 1-2 días pero solo de las alertas revisadas
y deja el resultado en la tabla etiquetas_conocidas (solo lo que se sabía antes del corte).

Si se contradicen manda el banco: un contracargo confirmado es definitivo, y así se ven los errores
de los analistas. Un "fraude" del analista que el banco no confirma se mantiene (parte del fraude
nunca se confirma). Lo que no está en la tabla cuenta como legítimo al recalibrar.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from recalibrar_modelo import conectar, reloj


def crear_tabla(client):
    client.command('''
        CREATE TABLE IF NOT EXISTS etiquetas_conocidas (
            transaccion_id  UInt32,
            es_fraude       UInt8,
            tipo_fraude     LowCardinality(Nullable(String)),
            fuente          LowCardinality(String),     -- banco, analista o banco+analista
            fecha_conocida  DateTime,                   -- cuándo se supo por primera vez
            corte           DateTime                    -- etiquetas conocidas hasta este momento
        ) ENGINE = MergeTree
        ORDER BY transaccion_id
    ''')


def con_columnas(df, tipos):
    """query_df devuelve un DataFrame sin columnas si no hay filas (p. ej. el banco aún no ha
    confirmado ningún fraude): se crea vacío con sus columnas y tipos para que el merge funcione."""
    if len(df):
        return df
    return pd.DataFrame({col: pd.Series(dtype=tipo) for col, tipo in tipos.items()})


def etiquetas_conocidas_a(client, corte):
    """Etiquetas que se conocían en `corte` (banco + analistas), sin escribir nada."""
    banco = client.query_df('''
        SELECT transaccion_id, tipo_fraude AS tipo_banco, fecha_confirmacion
        FROM transaccion
        WHERE es_fraude = 1 AND fecha_confirmacion <= {c:DateTime}
    ''', parameters={'c': corte})
    analistas = client.query_df('''
        SELECT transaccion_id, veredicto, tipo_fraude AS tipo_analista, fecha_revision
        FROM feedback_analistas
        WHERE fecha_revision <= {c:DateTime}
    ''', parameters={'c': corte})
    banco = con_columnas(banco, {'transaccion_id': 'uint32', 'tipo_banco': 'object',
                                 'fecha_confirmacion': 'datetime64[ns]'})
    analistas = con_columnas(analistas, {'transaccion_id': 'uint32', 'veredicto': 'object',
                                         'tipo_analista': 'object', 'fecha_revision': 'datetime64[ns]'})

    df = banco.merge(analistas, on='transaccion_id', how='outer')
    en_banco = df['fecha_confirmacion'].notna()
    en_analista = df['veredicto'].notna()
    df['es_fraude'] = (en_banco | (df['veredicto'] == 'fraude')).astype('uint8')
    df['tipo_fraude'] = df['tipo_banco'].where(en_banco, df['tipo_analista'])
    df['fuente'] = (en_banco.map({True: 'banco', False: ''})
                    + (en_banco & en_analista).map({True: '+', False: ''})
                    + en_analista.map({True: 'analista', False: ''}))
    df['fecha_conocida'] = df[['fecha_confirmacion', 'fecha_revision']].min(axis=1)
    df['corte'] = corte
    return df


def integrar(client, corte):
    crear_tabla(client)
    df = etiquetas_conocidas_a(client, corte)
    en_banco = df['fecha_confirmacion'].notna()
    en_analista = df['veredicto'].notna()

    client.command('TRUNCATE TABLE etiquetas_conocidas')
    columnas = ['transaccion_id', 'es_fraude', 'tipo_fraude', 'fuente', 'fecha_conocida', 'corte']
    if len(df):
        client.insert_df('etiquetas_conocidas', df[columnas])

    solo_analista = df[df['fuente'] == 'analista']
    errores = int((en_banco & (df['veredicto'] == 'legitima')).sum())
    print(f'Etiquetas conocidas a {corte:%Y-%m-%d}: {int(df["es_fraude"].sum())} fraudes '
          f'y {int((df["es_fraude"] == 0).sum())} legítimas revisadas')
    print(f'  confirmados por el banco:            {int(en_banco.sum()):>6}')
    print(f'  revisadas por analistas:             {int(en_analista.sum()):>6}')
    print(f'  fraudes que solo conocen analistas:  {int(solo_analista["es_fraude"].sum()):>6} '
          f'(el banco aún no los ha confirmado)')
    print(f'  analista dijo legítima, banco fraude: {errores:>5} (errores corregidos por el banco)')
    return df


def main():
    client = conectar()
    integrar(client, reloj(client))


if __name__ == '__main__':
    main()
