FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    ffmpeg v4l-utils adb alsa-utils libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py cc_remote.py ./
COPY web/ /app/web/

EXPOSE 5000 6489 8554
CMD ["python", "app.py"]
