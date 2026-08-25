FROM python:3.11-slim

WORKDIR /app

# Dependências primeiro (camada de cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Código da aplicação
COPY app.py receipt_collector.py wms_config.py ./
COPY static/ ./static/

# Diretório de dados persistentes (montado via volume no Fly.io / Render)
RUN mkdir -p /var/data

ENV PORT=8080

EXPOSE 8080

CMD ["sh", "-c", "gunicorn app:app --workers 1 --worker-class=gthread --threads 8 --timeout 0 --bind 0.0.0.0:$PORT"]
