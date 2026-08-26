FROM python:3.11-slim

# Install system runtime dependencies for curl_cffi and headless Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    git \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY requirements.txt pyproject.toml ./

# Install Python dependencies and pre-provision Chromium for Patchright/Playwright
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m patchright install chromium

# Copy application source code
COPY . .
# Expose container port
EXPOSE 8080

# Environment defaults
ENV PYTHONPATH=/app
ENV API_HOST=0.0.0.0
ENV DATABASE_URL=sqlite+aiosqlite:///./data/x_scraper.db
ENV EXPORTS_DIR=/app/exports
ENV PROXY_FILE_PATH=/app/proxies.txt

# Launch OrchisX server using platform dynamic PORT or default 8080
CMD ["sh", "-c", "orchis server --host 0.0.0.0 --port ${PORT:-8080}"]
