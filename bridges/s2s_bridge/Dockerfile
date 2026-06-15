# Edge container image for the s2s bridge (generic, glibc — not a Home Assistant
# add-on). Build, then run with your config from an env file:
#   docker build -t genesis-voice-s2s .
#   docker run --rm --env-file edge/.env -p 8080:8080 genesis-voice-s2s
FROM python:3.12-slim

WORKDIR /app

# ffmpeg for audio handling; build tools not needed (wheels available on glibc).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Runtime deps (kept in sync with deploy/install.sh). onnxruntime has real
# glibc wheels here, so no stub is needed (unlike the old HA Alpine image).
RUN pip install --no-cache-dir \
    "pipecat-ai[openai,websocket]==1.3.0" openai "websockets>=13.0" \
    "fastapi>=0.115.0" "uvicorn[standard]>=0.23.0" python-dotenv httpx \
    aiohttp aiofiles pydantic loguru numpy Pillow protobuf nltk Markdown \
    soxr pyloudnorm docstring_parser onnxruntime

COPY app/ /app/app/

ENV PYTHONUNBUFFERED=1
# Configure via --env-file edge/.env (see edge/.env.example). Default port 8080.
EXPOSE 8080
CMD ["python", "-m", "app.main"]
