"""
La base de datos de la aplicación: cuentas, sesiones, códigos de 6 cifras, Mis avisos y empresas registradas.

Va aparte de ClickHouse, que es para el análisis (pagos, alertas, scoring), como en cualquier aplicación real:
aquí se escribe a menudo poco y hay que guardarlo de verdad, allí se leen millones de filas.

  - CUENTAS_URL=postgresql://...  (en .env, en Render y en los secretos de GitHub Actions): una PostgreSQL en la
    nube (Neon, gratis) compartida por todos: la web de cada uno, la publicada y el scoring de la noche. Lo que se
    cambia en cualquiera de ellas se guarda para todas.
  - Sin CUENTAS_URL, o CUENTAS_URL=sqlite:///ruta: un archivo SQLite (por defecto web/cuentas.sqlite, fuera de Git).
    Para trabajar sin internet y para los tests, que nunca tocan la base de datos real.

Las fechas se guardan como segundos desde 1970 (UTC): igual en las dos bases de datos y sin líos de zonas horarias.
Solo usa SQL que entienden las dos (INSERT ... ON CONFLICT, parámetros con ?).
"""
import os
import sqlite3
import threading
import time
from pathlib import Path

ARCHIVO_LOCAL = Path(__file__).resolve().parent / 'cuentas.sqlite'

TABLAS = [
    '''CREATE TABLE IF NOT EXISTS usuarios (
        email              TEXT PRIMARY KEY,
        nombre             TEXT NOT NULL,
        rol                TEXT NOT NULL,          -- administrador, analista o comercio
        comercio_id        INTEGER NOT NULL DEFAULT 0,
        sal                TEXT NOT NULL,
        hash               TEXT NOT NULL,          -- PBKDF2-SHA256 de la contraseña
        estado             TEXT NOT NULL,          -- activa o desactivada
        telefono           TEXT NOT NULL DEFAULT '',
        nota               TEXT NOT NULL DEFAULT '',
        creada             BIGINT NOT NULL,
        revisada_por       TEXT NOT NULL DEFAULT '',
        cambiar_contrasena INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS sesiones (
        token   TEXT PRIMARY KEY,
        email   TEXT NOT NULL,
        caduca  BIGINT NOT NULL,
        cerrada INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX IF NOT EXISTS sesiones_email ON sesiones (email)',
    '''CREATE TABLE IF NOT EXISTS codigos (
        email       TEXT NOT NULL,
        proposito   TEXT NOT NULL,                 -- recuperar o registro
        codigo_hash TEXT NOT NULL,
        datos       TEXT NOT NULL DEFAULT '',
        caduca      BIGINT NOT NULL,
        intentos    INTEGER NOT NULL DEFAULT 0,
        usado       INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (email, proposito))''',
    '''CREATE TABLE IF NOT EXISTS ajustes_usuario (
        email TEXT NOT NULL,
        clave TEXT NOT NULL,
        valor TEXT NOT NULL,
        PRIMARY KEY (email, clave))''',
    '''CREATE TABLE IF NOT EXISTS empresa (
        comercio_id INTEGER PRIMARY KEY,
        nombre      TEXT NOT NULL,
        cif         TEXT NOT NULL DEFAULT '',
        categoria   TEXT NOT NULL DEFAULT '',
        pais        TEXT NOT NULL DEFAULT '',
        ciudad      TEXT NOT NULL DEFAULT '',
        direccion   TEXT NOT NULL DEFAULT '',
        telefono    TEXT NOT NULL DEFAULT '',
        web         TEXT NOT NULL DEFAULT '',
        registrada  INTEGER NOT NULL DEFAULT 0,  -- se registró desde la web (si no, viene de los datos de la entidad)
        configurada INTEGER NOT NULL DEFAULT 0)''',
]


def ahora():
    return int(time.time())


def direccion():
    return os.environ.get('CUENTAS_URL', '').strip() or f'sqlite:///{ARCHIVO_LOCAL}'


class BaseCuentas:
    """Conexión a la base de datos de cuentas. Una conexión por hilo (la web atiende varias peticiones a la vez)."""

    def __init__(self, url=None):
        self.url = url or direccion()
        self.postgres = self.url.startswith(('postgres://', 'postgresql://'))
        self._hilo = threading.local()
        self._lista = False

    def __repr__(self):
        return f'BaseCuentas({"PostgreSQL" if self.postgres else self.url})'

    def _conectar(self):
        if self.postgres:
            import psycopg
            return psycopg.connect(self.url, autocommit=True, connect_timeout=15)
        ruta = self.url.removeprefix('sqlite:///')
        Path(ruta).parent.mkdir(parents=True, exist_ok=True)
        conexion = sqlite3.connect(ruta, timeout=30, isolation_level=None, check_same_thread=False)
        conexion.execute('PRAGMA journal_mode=WAL')
        return conexion

    def _conexion(self):
        if getattr(self._hilo, 'conexion', None) is None:
            self._hilo.conexion = self._conectar()
        return self._hilo.conexion

    def _ejecutar(self, sql, parametros=()):
        sql_final = sql.replace('?', '%s') if self.postgres else sql
        for intento in (1, 2):
            try:
                cursor = self._conexion().cursor()
                cursor.execute(sql_final, tuple(parametros))
                return cursor
            except Exception as e:
                # Neon cierra las conexiones que llevan un rato sin usarse: se reconecta una vez
                cerrada = self.postgres and type(e).__name__ in ('OperationalError', 'InterfaceError')
                if intento == 2 or not cerrada:
                    raise
                self._hilo.conexion = None

    def preparar(self):
        if not self._lista:
            for sql in TABLAS:
                self._ejecutar(sql)
            self._lista = True

    def ejecutar(self, sql, parametros=()):
        self.preparar()
        self._ejecutar(sql, parametros)

    def filas(self, sql, parametros=()):
        """Las filas como diccionarios."""
        self.preparar()
        cursor = self._ejecutar(sql, parametros)
        columnas = [c[0] for c in cursor.description]
        return [dict(zip(columnas, f)) for f in cursor.fetchall()]

    def fila(self, sql, parametros=()):
        filas = self.filas(sql, parametros)
        return filas[0] if filas else None

    def guardar(self, tabla, fila, clave):
        """Inserta la fila o, si ya existe (misma clave), la reemplaza."""
        columnas = list(fila)
        clave = [clave] if isinstance(clave, str) else list(clave)
        resto = [c for c in columnas if c not in clave]
        sql = (f'INSERT INTO {tabla} ({", ".join(columnas)}) VALUES ({", ".join("?" * len(columnas))}) '
               f'ON CONFLICT ({", ".join(clave)}) DO ' +
               (f'UPDATE SET {", ".join(f"{c} = excluded.{c}" for c in resto)}' if resto else 'NOTHING'))
        self.ejecutar(sql, [fila[c] for c in columnas])
