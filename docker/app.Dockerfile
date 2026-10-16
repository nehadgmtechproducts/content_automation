# Base image for api / worker / beat.
# Python 3.11, not 3.14 — spaCy, faster-whisper and sentence-transformers wheels
# lag the newest interpreter on arm64.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
# CPU-only PyTorch first. The default PyPI wheel on Linux drags in ~3GB of NVIDIA CUDA
# libraries that a CPU server never uses; sentence-transformers then sees torch as already
# installed and doesn't pull the CUDA build.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt \
    && python -m spacy download en_core_web_sm

COPY . .
RUN mkdir -p /srv/media /models
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
