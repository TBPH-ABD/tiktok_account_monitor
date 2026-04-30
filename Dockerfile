FROM mcr.microsoft.com/playwright/python:v1.56.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY tiktok_monitor.py .

CMD ["python3", "tiktok_monitor.py"]
