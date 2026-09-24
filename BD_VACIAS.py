import clickhouse_connect
 
HOST = 'localhost'
PORT = 8123
USER = 'default'
PASSWORD = 'password'  #
DATABASE = 'fraude_pagos'

 
def get_client(database=None):
    return clickhouse_connect.get_client(
        host=HOST, port=PORT, username=USER, password=PASSWORD, database=database
    )
 
 
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
            timestamp       DateTime
        ) ENGINE = MergeTree()
        ORDER BY cliente_id
    '''
 
    tablas['metodo_pago'] = '''
        CREATE TABLE IF NOT EXISTS metodo_pago (
            metodo_id           UInt32,
            cliente_id          UInt32,  
            numero_enmascarado  String,
            tipo                String,
            entidad_emisora     String,
            fecha_expiracion    Date,
            limite_credito      Nullable(Decimal(10, 2)),
            estado              String
        ) ENGINE = MergeTree()
        ORDER BY metodo_id
    '''
 
    tablas['comercio'] = '''
        CREATE TABLE IF NOT EXISTS comercio (
            comercio_id       UInt32,
            categoria_negocio String,
            pais              String,
            nombre            String,
            ciudad            String
        ) ENGINE = MergeTree()
        ORDER BY comercio_id
    '''
 
    tablas['canal_pago'] = '''
        CREATE TABLE IF NOT EXISTS canal_pago (
            canal_id    UInt32,
            comercio_id UInt32,  
            tipo        String,
            ubicacion   String
        ) ENGINE = MergeTree()
        ORDER BY canal_id
    '''
 
    tablas['dispositivo'] = '''
        CREATE TABLE IF NOT EXISTS dispositivo (
            dispositivo_id     UInt32,
            tipo               String,
            modelo             String,
            sistema_operativo  String
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
            dispositivo_id UInt32    
        ) ENGINE = MergeTree()
        ORDER BY (cliente_id, dispositivo_id)
    '''

 
    tablas['sesion'] = '''
        CREATE TABLE IF NOT EXISTS sesion (
            sesion_id           UInt32,
            cliente_id          UInt32,   
            dispositivo_id      UInt32,   
            ip_sesion           String,
            ip_pais             String,
            proxy_vpn           Bool,
            num_intentos_login  UInt8,
            resultado_login     String,
            timestamp           DateTime
        ) ENGINE = MergeTree()
        ORDER BY (timestamp, sesion_id)
    '''
 
    tablas['evento'] = '''
        CREATE TABLE IF NOT EXISTS evento (
            evento_id  UInt32,
            sesion_id  UInt32,   
            tipo       String,
            timestamp  DateTime,
            detalle    String
        ) ENGINE = MergeTree()
        ORDER BY (timestamp, evento_id)
    '''
 
    tablas['transaccion'] = '''
        CREATE TABLE IF NOT EXISTS transaccion (
            transaccion_id      UInt32,
            metodo_id           UInt32,  
            canal_id            UInt32,  
            dispositivo_id      UInt32,   
            sesion_id           UInt32,   
            timestamp           DateTime,
            cantidad            Decimal(10, 2),
            moneda              String,
            tipo_operacion      String,
            metodo_autenticacion String,
            estado              String
        ) ENGINE = MergeTree()
        ORDER BY (timestamp, transaccion_id)
    '''
 
    tablas['devolucion'] = '''
        CREATE TABLE IF NOT EXISTS devolucion (
            devolucion_id  UInt32,
            transaccion_id UInt32, 
            tipo           String,
            motivo         String,
            importe        Decimal(10, 2),
            fecha          DateTime,
            estado         String
        ) ENGINE = MergeTree()
        ORDER BY (fecha, devolucion_id)
    '''
 
    tablas['alerta'] = '''
        CREATE TABLE IF NOT EXISTS alerta (
            alerta_id      UInt32,
            transaccion_id UInt32,  
            patron_id      UInt32,  
            nota_riesgo    Float32,
            fecha          DateTime,
            estado         String
        ) ENGINE = MergeTree()
        ORDER BY (fecha, alerta_id)
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
 