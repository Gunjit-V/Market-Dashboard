import os
import secrets

from fastapi import Header, HTTPException

from db.connection import ConnectionManager

manager = ConnectionManager()


def get_db():
    """FastAPI dependency that provides a database connection."""
    conn = manager.get_connection()
    try:
        yield conn
    finally:
        conn.close()


def require_api_key(x_api_key: str = Header(default="")):
    """Gate mutating endpoints (triggering downloads/backtests) behind a
    shared API key. Unset API_KEY disables the check — fine for local dev,
    but must be set before exposing this API beyond localhost."""
    expected = os.getenv("API_KEY")
    if not expected:
        return
    if not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
