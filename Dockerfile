FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

EXPOSE ${API_PORT:-8099}

CMD python -m uvicorn app.main:app --host ${API_HOST:-0.0.0.0} --port ${API_PORT:-8099}
