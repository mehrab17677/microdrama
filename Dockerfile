FROM python:3.12-slim-bookworm

# Debian's ffmpeg links libass (with harfbuzz and fribidi) and libx264.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg fonts-noto-core fontconfig curl \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/microdrama
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

RUN useradd -m app && mkdir -p /tmp/microdrama && chown -R app /tmp/microdrama
USER app
EXPOSE 8000
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000"]
