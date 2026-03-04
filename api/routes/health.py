from fastapi import APIRouter, Depends
from api.dependencies import get_db
from api.models.schemas import Response

router = APIRouter()


@router.get("", response_model=Response)
def health_check(conn=Depends(get_db)):
    """Check API and database connectivity."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()[0]

        return Response(
            status="success",
            message="API is healthy",
            data={
                "api": "running",
                "database": "connected",
                "postgres_version": version,
            }
        )

    except Exception as e:
        return Response(
            status="error",
            message=f"Database connection failed: {str(e)}",
            data={
                "api": "running",
                "database": "disconnected",
            }
        )