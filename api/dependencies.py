from db.connection import ConnectionManager

manager = ConnectionManager()


def get_db():
    """FastAPI dependency that provides a database connection."""
    conn = manager.get_connection()
    try:
        yield conn
    finally:
        conn.close()
