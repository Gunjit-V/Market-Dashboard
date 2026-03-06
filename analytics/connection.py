import os
import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv

load_dotenv()

DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_HOST = os.getenv("DB_HOST")
DB_PORT = int(os.getenv("DB_PORT"))


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
