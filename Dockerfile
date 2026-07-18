FROM debian:bookworm-slim

# Install system deps — ffmpeg from bookworm-backports for v4l2-request on arm64
RUN echo "deb http://deb.debian.org/debian bookworm-backports main" >> /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y -t bookworm-backports \
        ffmpeg v4l-utils adb alsa-utils libasound2-dev \
        python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Verify v4l2 encoder is available (especially on arm64/RPi)
RUN ffmpeg -encoders 2>/dev/null | grep -qi v4l2 \
    && echo "✅ v4l2 encoder found" \
    || echo "ℹ️ No v4l2 encoder (expected on x86_64 builds — libx264 fallback works)"

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY app.py cc_remote.py ./
COPY web/ /app/web/

EXPOSE 5000 6489 8554
CMD ["python", "app.py"]
