import clickhouse_connect
 
from config import HOST, PORT, USER, PASSWORD, DATABASE
 
def get_client(database=None):
    return clickhouse_connect.get_client(host=HOST, port=PORT, username=USER, password=PASSWORD, database=database )
 
 
def crear_base_de_datos():
    client = get_client()
    client.command(f'CREATE DATABASE IF NOT EXISTS {DATABASE}')
    print(f'Base de datos "{DATABASE}" lista.')
 
 
def crear_tablas(client):
    tablas = {}
    tablas['cliente'] = '''
        CREATE TABLE IF NOT EXISTS cliente (
            cliente_id      UInt32,
            nombre_completo String,
            email           String,
            pais            LowCardinality(String),
            fecha_alta      DateTime
        ) ENGINE = MergeTree()
        ORDER BY cliente_id
    '''
 
    tablas['metodo_pago'] = '''
        CREATE TABLE IF NOT EXISTS metodo_pago (
            metodo_id           UInt32,
            cliente_id          UInt32,                  
            numero_enmascarado  String,
            tipo                LowCardinality(String),
            entidad_emisora     LowCardinality(String),
            fecha_expiracion    Date,
            limite_credito      Nullable(Decimal(10, 2)),
            estado              LowCardinality(String)
        ) ENGINE = MergeTree()
        ORDER BY metodo_id
    '''
 
    tablas['comercio'] = '''
        CREATE TABLE IF NOT EXISTS comercio (
            comercio_id       UInt32,
            nombre            String,
            categoria_negocio LowCardinality(String),
            pais              LowCardinality(String),
            ciudad            LowCardinality(String)
        ) ENGINE = MergeTree()
        ORDER BY comercio_id
    '''
 
    tablas['canal_pago'] = '''
        CREATE TABLE IF NOT EXISTS canal_pago (
            canal_id    UInt32,
            comercio_id UInt32,                  
            tipo        LowCardinality(String),  
            ubicacion   String
        ) ENGINE = MergeTree()
        ORDER BY canal_id
    '''
 
    tablas['dispositivo'] = '''
        CREATE TABLE IF NOT EXISTS dispositivo (
            dispositivo_id     UInt32,
            tipo               LowCardinality(String),
            modelo             LowCardinality(String),
            sistema_operativo  LowCardinality(String)
        ) ENGINE = MergeTree()
        ORDER BY dispositivo_id
    '''
 
    tablas['patron'] = '''
        CREATE TABLE IF NOT EXISTS patron (
            patron_id    UInt32,
            nombre       String,
            descripcion  String,
            condicion    String
        ) ENGINE = MergeTree()
        ORDER BY patron_id
    '''
 
    
 
    tablas['cliente_dispositivo'] = '''
        CREATE TABLE IF NOT EXISTS cliente_dispositivo (
            cliente_id     UInt32, 
            dispositivo_id UInt32,  
        ) ENGINE = MergeTree()
        ORDER BY (cliente_id, dispositivo_id)
    '''
 
 
    tablas['sesion'] = '''
        CREATE TABLE IF NOT EXISTS sesion (
            sesion_id           UInt32,
            cliente_id          UInt32,                  
            dispositivo_id      UInt32,                 
            ip_sesion           String,
            ip_pais             LowCardinality(String),
            proxy_vpn           Bool,
            num_intentos_login  UInt8,
            resultado_login     LowCardinality(String),
            timestamp           DateTime
        ) ENGINE = MergeTree()
        ORDER BY (cliente_id, timestamp)
    '''
 
    tablas['evento'] = '''
        CREATE TABLE IF NOT EXISTS evento (
            evento_id  UInt32,
            sesion_id  UInt32,                 
            tipo       LowCardinality(String),
            timestamp  DateTime,
            detalle    String
        ) ENGINE = MergeTree()
        ORDER BY (sesion_id, timestamp)
    '''
 
    tablas['transaccion'] = '''
        CREATE TABLE IF NOT EXISTS transaccion (
            transaccion_id       UInt32,
            metodo_id            UInt32,                          
            canal_id             UInt32,                          
            dispositivo_id       Nullable(UInt32),                
            sesion_id            Nullable(UInt32),                
            timestamp            DateTime,
            cantidad             Decimal(10, 2),
            moneda               LowCardinality(String),
            tipo_operacion       LowCardinality(String),
            metodo_autenticacion LowCardinality(String),
            estado               LowCardinality(String),
            es_fraude            UInt8,                           
            tipo_fraude          LowCardinality(Nullable(String)) 
        ) ENGINE = MergeTree()
        ORDER BY (metodo_id, timestamp)
    '''
 
    tablas['devolucion'] = '''
        CREATE TABLE IF NOT EXISTS devolucion (
            devolucion_id  UInt32,
            transaccion_id UInt32,                  
            tipo           LowCardinality(String),
            motivo         LowCardinality(String),
            importe        Decimal(10, 2),
            fecha          DateTime,
            estado         LowCardinality(String)
        ) ENGINE = MergeTree()
        ORDER BY (transaccion_id, fecha)
    '''
 
    tablas['alerta'] = '''
        CREATE TABLE IF NOT EXISTS alerta (
            alerta_id       UInt32,
            transaccion_id  UInt32,                           
            origen          LowCardinality(String),           
            patron_id       Nullable(UInt32),                                
            nota_riesgo     Float32,
            fecha           DateTime,
            estado          LowCardinality(String),                           
            analista_id     Nullable(UInt32),
            fecha_revision  Nullable(DateTime),
            veredicto       LowCardinality(Nullable(String)),
            comentario      Nullable(String)
        ) ENGINE = MergeTree()
        ORDER BY (fecha, alerta_id)
    '''

    
    tablas['analista'] = '''
        CREATE TABLE IF NOT EXISTS analista (
            analista_id  UInt32,
            nombre       String,
            email        String,
            nivel        LowCardinality(String),
            fecha_alta   DateTime,
            activo       Bool
        ) ENGINE = MergeTree()
        ORDER BY analista_id
    '''
 
    for nombre, ddl in tablas.items():
        client.command(ddl)
        print(f'  Tabla "{nombre}" creada (o ya existía).')
 
 
def verificar(client):
    resultado = client.query(f'SHOW TABLES FROM {DATABASE}')
    print(f'\nTablas en "{DATABASE}":')
    for fila in resultado.result_rows:
        print(f'  - {fila[0]}')
 
 
if __name__ == '__main__':
    crear_base_de_datos()
    client = get_client(database=DATABASE)
    print('\nCreando tablas...')
    crear_tablas(client)
    verificar(client)
 