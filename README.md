# Indian Stock Market Data Platform

A comprehensive algorithmic trading and market data collection platform designed for the Indian stock market. The project leverages Angel One's SmartAPI to download historical OHLCV data, real-time ticks, and instruments, and provides a powerful FastAPI backend along with a live WebSocket-powered dashboard to visualize the data.

## 🚀 Features

*   **Market Data Downloader (`downloader/`)**: Robust scripts to fetch FO instruments, daily OHLCV data, and real-time market ticks using the Angel One SmartAPI.
*   **FastAPI Backend (`api/`)**: A high-performance RESTful API serving market data (Instruments, OHLCV, Ticks, Volatility metrics).
*   **Live Dashboard (`dashboard/`)**: A WebSocket-enabled live dashboard that processes and visualizes real-time market data without expensive polling mechanisms.
*   **Frontend Client (`frontend/`)**: React-based frontend client for rich data exploration and visualizations.
*   **Database (`db/`)**: PostgreSQL integration to persist and manage vast amounts of tick and OHLCV data efficiently.
*   **Scheduler (`scheduler/`)**: Automated job scheduling for routine data extraction and maintenance tasks.

## 🛠️ Tech Stack

*   **Backend & API**: Python 3.x, FastAPI, Uvicorn
*   **Market Data Source**: Angel One SmartAPI (`smartapi-python`)
*   **Database**: PostgreSQL (`psycopg2`)
*   **Real-time Communication**: WebSockets (`websocket-client`)
*   **Frontend**: React.js (in `frontend/`), Vanilla HTML/JS (in `dashboard/`)
*   **Data Processing**: Pandas, NumPy

## 📁 Project Structure

```bash
.
├── api/             # FastAPI application and route definitions
├── dashboard/       # Simple WebSocket-powered live dashboard client & server
├── data/            # Local data storage and exports
├── db/              # Database models, schemas, and connection utilities
├── downloader/      # Scripts for tick collection, OHLCV fetching, and instruments loading
├── frontend/        # React frontend application
├── logs/            # Application and script log files
├── scheduler/       # Cron jobs and automated pipeline scripts
└── .env             # Environment variables (API keys, DB credentials)
```

## ⚙️ Installation & Setup

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/yourusername/your-repo-name.git
    cd your-repo-name
    ```

2.  **Set Up the Python Environment**
    It is recommended to use a virtual environment.
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows use: venv\Scripts\activate
    pip install -r requirements.txt
    ```

3.  **Environment Variables**
    Create a `.env` file in the root directory and add your credentials:
    ```env
    # SmartAPI Credentials
    ANGEL_API_KEY=your_api_key
    ANGEL_CLIENT_ID=your_client_id
    ANGEL_PASSWORD=your_password
    ANGEL_TOTP_SECRET=your_totp_secret
    
    # Database Settings
    DATABASE_URL=postgresql://user:password@localhost:5432/dbname
    ```

4.  **Database Migration (Optional/If applicable)**
    Run the necessary SQL scripts within `db/` to initialize tables.
    ```bash
    # e.g., psql -d dbname -f db/schema.sql
    ```

## 🎯 Running the Application

### 1. Market Data API
To start the FastAPI backend server:
```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```
*   **Swagger Documentation**: `http://localhost:8000/docs`
*   **Health Check**: `http://localhost:8000/health/`

### 2. Live WebSocket Dashboard
To run the live dashboard server:
```bash
python dashboard/server.py
```
Then open `dashboard/live_dashboard.html` in your browser.

### 3. Real-time Ticks Downloader
To start collecting ticks from SmartAPI:
```bash
python downloader/tick_downloader.py
```

## 🤝 Contributing

Contributions are always welcome. Please feel free to open an issue or submit a Pull Request.

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.
