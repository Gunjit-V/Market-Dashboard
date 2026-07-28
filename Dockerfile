# Use the official Python base image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Set the working directory
WORKDIR /app

# Install system dependencies (needed for psycopg2, etc)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    python3-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Ensure fastapi and uvicorn are installed just in case they were missed
RUN pip install --no-cache-dir fastapi uvicorn

# Copy the rest of the application code
COPY . .

# The CMD will be overridden by docker-compose for each specific service
# (api, dashboard, tick-downloader)
CMD ["python", "--version"]
