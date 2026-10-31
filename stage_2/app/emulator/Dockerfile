FROM python:3.11-slim

RUN useradd --create-home appuser
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
USER appuser

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
