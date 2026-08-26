FROM python:3.11-slim

# Install system dependencies for curl_cffi and build tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications
COPY pyproject.toml requirements.txt ./

# Install Python dependencies
RUN pip install --no-cache-dir -e .

# Copy application source code
COPY . .

# Create exports directory and persistent data mount points
RUN mkdir -p /app/exports

# Expose API and Web Dashboard port
EXPOSE 8100

# Environment defaults
ENV API_HOST=0.0.0.0
ENV API_PORT=8100
ENV DATABASE_URL=sqlite+aiosqlite:///./x_scraper.db

# Run OrchisX server with uvicorn
CMD ["orchis", "server", "--host", "0.0.0.0", "--port", "8100"]
