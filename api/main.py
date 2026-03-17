from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import health, instruments, ohlcv, ticks, download, volatility

app = FastAPI(
    title="Indian Stock Market Data",
    description="API for accessing Indian Stock market data",
    version="0.1.0",
)

# CORS — allows React frontend to talk to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten this when moving to production
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


@app.get("/")
def root():
    return {
        "status": "success",
        "message": "Indian Stock Market Data API is running",
    }
