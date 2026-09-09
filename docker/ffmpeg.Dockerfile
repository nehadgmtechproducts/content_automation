# Lightweight render helper for local development.
#
# The host's Homebrew ffmpeg is built WITHOUT libass/freetype — `ass`, `subtitles` and
# `drawtext` are all missing — so kinetic subtitles cannot be burned there at all.
# This image exists purely to provide a working ffmpeg; set RENDER_IN_DOCKER=1 and
# app/assets/render.py will delegate to it, mounting the media tree at the same
# absolute path so the generated command runs unchanged.
#
#   docker build -t content-engine-ffmpeg -f docker/ffmpeg.Dockerfile .
#
# In production the full worker image (docker/render.Dockerfile) does this internally.
FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/* && fc-cache -f
# Fail the build rather than ship an image that silently drops subtitles.
RUN ffmpeg -hide_banner -filters | grep -qw ass \
 && ffmpeg -hide_banner -filters | grep -qw zoompan \
 && echo "ffmpeg: ass + zoompan present"
