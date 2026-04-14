FROM python:3.12-slim

# Install Chromium and dependencies for headless browser OAuth
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    libnss3 \
    libatk-bridge2.0-0 \
    libdrm2 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libcups2 \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Prevent pyppeteer from downloading its own Chromium
ENV PYPPETEER_CHROMIUM_REVISION=""
ENV PYPPETEER_NO_CHROMIUM_DOWNLOAD=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE 8099

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
