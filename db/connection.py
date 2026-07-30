import os
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv
from urllib.parse import unquote, urlparse

load_dotenv()


def _database_config() -> dict:
    """Resolve PostgreSQL settings from DATABASE_URL or individual vars."""
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        parsed = urlparse(database_url)
        if parsed.scheme not in {"postgres", "postgresql"}:
            raise ValueError("DATABASE_URL must use the postgres:// or postgresql:// scheme")
        return {
            "dbname": unquote(parsed.path.lstrip("/")) or "dbname",
            "user": unquote(parsed.username) if parsed.username else "user",
            "password": unquote(parsed.password) if parsed.password else "password",
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 5432,
        }

    return {
        "dbname": os.getenv("DB_NAME") or os.getenv("POSTGRES_DB") or "dbname",
        "user": os.getenv("DB_USER") or os.getenv("POSTGRES_USER") or "user",
        "password": os.getenv("DB_PASSWORD") or os.getenv("POSTGRES_PASSWORD") or "password",
        "host": os.getenv("DB_HOST") or "localhost",
        "port": int(os.getenv("DB_PORT") or 5432),
    }


_DB_CONFIG = _database_config()
DB_NAME = _DB_CONFIG["dbname"]
DB_USER = _DB_CONFIG["user"]
DB_PASSWORD = _DB_CONFIG["password"]
DB_HOST = _DB_CONFIG["host"]
DB_PORT = _DB_CONFIG["port"]


class ConnectionManager:
    """Manage connections to the PostgreSQL database using env vars."""

    def __init__(
        self,
        dbname: str | None = None,
        user: str | None = None,
        password: str | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.dbname = dbname or DB_NAME
        self.user = user or DB_USER
        self.password = password or DB_PASSWORD
        self.host = host or DB_HOST
        self.port = port or DB_PORT

    def get_connection(self):
        """Return a new psycopg2 connection."""
        return psycopg2.connect(
            dbname=self.dbname,
            user=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
        )


if __name__ == "__main__":
    # Simple test of the connection manager
    try:
        manager = ConnectionManager()
        with manager.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT version();"))
                version = cur.fetchone()
                print("Connected to PostgreSQL:", version[0])
        conn.close()
    except Exception as e:
        print("Error connecting to PostgreSQL:", e)
