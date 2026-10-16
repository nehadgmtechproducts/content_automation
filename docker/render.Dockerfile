# Render worker.
#
# The host's Homebrew ffmpeg is built WITHOUT libass/freetype — `ass`, `subtitles` and
# `drawtext` are all missing, so kinetic subtitles cannot be burned there. Debian's
# ffmpeg package is built with libass, which is the entire reason this image exists.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fontconfig \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -f

# Fail the build rather than ship an image that silently drops subtitles.
RUN ffmpeg -hide_banner -filters | grep -qw ass \
    && ffmpeg -hide_banner -filters | grep -qw zoompan \
    && echo "ffmpeg: ass + zoompan present"

# Piper TTS as a standalone binary — the pip package needs piper-phonemize, which
# publishes no wheel for macOS arm64, so the binary is the only portable path.
ARG PIPER_VERSION=2023.11.14-2
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        arm64) piper_arch=aarch64 ;; \
        amd64) piper_arch=x86_64 ;; \
        *) echo "unsupported arch $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/piper.tar.gz \
        "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_${piper_arch}.tar.gz"; \
    tar -xzf /tmp/piper.tar.gz -C /opt; \
    ln -s /opt/piper/piper /usr/local/bin/piper; \
    rm /tmp/piper.tar.gz; \
    piper --help > /dev/null 2>&1 || true

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
CMD ["celery", "-A", "app.tasks.celery_app", "worker", "--loglevel=info", "--concurrency=1"]
