# Transcriber queue daemon — Python 3.12 slim + ffmpeg + tzdata
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir google-genai faster-whisper "av<19"

COPY src/ /app/

# Whisper model cache volume keeps the ~460 MB model across restarts.
VOLUME ["/var/lib/transcriber", "/root/.cache"]

CMD ["python", "/app/transcriberd.py", "run"]
