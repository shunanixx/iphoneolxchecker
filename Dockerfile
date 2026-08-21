FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# lxml needs a compiler toolchain only if no wheel is available; keeping
# the build deps in a separate layer keeps rebuilds fast.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# The SQLite file lives on a mounted volume, not in the image.
RUN mkdir -p /app/data

# Non-root user for the actual process; entrypoint.sh starts as root
# (needed to fix up the bind-mounted ./data ownership, see its comment)
# and drops to this user before exec'ing the app.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "bot.main"]
