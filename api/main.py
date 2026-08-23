import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import health, instruments, ohlcv, ticks, download, volatility, strategies, paper_trading

app = FastAPI(
    title="Indian Stock Market Data",
    description="API for accessing Indian Stock market data",
    version="0.1.0",
)

# CORS — allows the frontend to talk to this API. Defaults to local dev
# origins; set CORS_ALLOWED_ORIGINS (comma-separated) to restrict this once
# the frontend is deployed somewhere other than localhost.
_default_origins = "http://localhost:3000,http://localhost:5173,http://127.0.0.1:3000,http://127.0.0.1:5173"
allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", _default_origins).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(health.router, prefix="/health", tags=["Health"])
app.include_router(instruments.router,
                   prefix="/instruments", tags=["Instruments"])
app.include_router(ohlcv.router, prefix="/ohlcv", tags=["OHLCV"])
app.include_router(ticks.router, prefix="/ticks", tags=["Ticks"])
app.include_router(download.router, prefix="/download", tags=["Download"])
app.include_router(volatility.router, prefix="/volatility",
                   tags=["Volatility"])
app.include_router(strategies.router, prefix="/strategies", tags=["Strategies"])
app.include_router(paper_trading.router, prefix="/paper-trading", tags=["Paper Trading"])


@app.get("/")
def root():
    return {
        "status": "success",
        "message": "Indian Stock Market Data API is running",
    }
