FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    adb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/cc_remote.py .

EXPOSE 5001
CMD ["python", "cc_remote.py"]
