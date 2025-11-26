FROM python:3.11-slim

# Install FFmpeg and git (sometimes needed for yt-dlp dependencies)
RUN apt-get update && \
    apt-get install -y ffmpeg git && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render sets PORT env var, but we default to 10000. 
# Timeout set to 600s (10 mins) because video processing is slow.
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 600 app:app
