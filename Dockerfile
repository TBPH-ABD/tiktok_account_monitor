FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tiktok_monitor.py .

CMD ["python3", "tiktok_monitor.py"]
