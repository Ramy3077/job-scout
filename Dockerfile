# Hosted demo image: serve.py in demo mode, with tectonic so "Build CV" produces a real PDF.
FROM python:3.12-slim

ARG TECTONIC_VERSION=0.17.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && arch="$(uname -m)" \
 && case "$arch" in aarch64) t=aarch64-unknown-linux-musl ;; *) t=x86_64-unknown-linux-musl ;; esac \
 && curl -fsSL "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40${TECTONIC_VERSION}/tectonic-${TECTONIC_VERSION}-${t}.tar.gz" \
    | tar -xz -C /usr/local/bin tectonic \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

ENV JOBSCOUT_DEMO=1 \
    JOBSCOUT_HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

RUN useradd --create-home app
WORKDIR /app
RUN chown app:app /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=app:app . .
USER app

# Compile the sample CVs once at build time. This also warms tectonic's package cache, so the
# running demo does not depend on fetching fonts the first time someone builds a CV.
RUN python3 jobscout/demo.py --prebuild

# Railway sets PORT. Locally: docker run -p 7777:7777 ... serves on 7777.
CMD ["sh", "-c", "JOBSCOUT_PORT=${PORT:-7777} exec python3 serve.py"]
