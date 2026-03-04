# Indian Stock Market — React Frontend

Vite + React + TypeScript app that talks to the FastAPI backend.

## Setup

```bash
cd frontend
npm install
```

## Run

Start the backend (e.g. `uvicorn api.main:app --reload` on port 8000), then:

```bash
npm run dev
```

App runs at **http://localhost:3000**. Vite proxies `/api` to `http://localhost:8000` so the frontend can call the backend without CORS issues.

## Build

```bash
npm run build
npm run preview   # serve dist/
```

## Env

Optional: create `.env` with `VITE_API_URL=http://localhost:8000` if the API is on another origin (no proxy).
