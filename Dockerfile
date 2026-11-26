# AWS EC2 (or any container host). FFmpeg comes from imageio-ffmpeg, so no system package is needed.
# Use the instance's IAM role for S3 and AWS Transcribe. Do not put access keys in the image.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    CLIPPER_MODE=aws CLIPPER_HOST=0.0.0.0 CLIPPER_PORT=8000 CLIPPER_DATA_DIR=/data

WORKDIR /app
# Captions are burned in with libass, which needs fonts. Without them, captions can disappear.
RUN apt-get update && apt-get install -y --no-install-recommends fontconfig fonts-liberation \n    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt boto3 "psycopg[binary]"
COPY . .
RUN useradd --create-home clipper && mkdir -p /data && chown clipper /data
USER clipper
VOLUME ["/data"]
EXPOSE 8000
CMD ["python", "-m", "clipper", "serve"]
