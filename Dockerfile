FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations
COPY scripts/vendor_htmx.py ./scripts/vendor_htmx.py
# HTMX must be served locally (no CDN at runtime). Use the committed copy, download it only if it is
# missing, then FAIL the build unless the file exists and matches the pinned SHA-384 checksum.
RUN python scripts/vendor_htmx.py --if-missing && python scripts/vendor_htmx.py --verify

# No secrets are copied or baked in: configuration arrives as environment variables at run time.
RUN useradd --system --create-home --uid 10001 poster \
    && mkdir -p /srv/data && chown -R poster:poster /srv/data
USER poster

EXPOSE 8000
# One image, several processes; docker-compose overrides CMD for the worker and the migration job.
CMD ["uvicorn", "--factory", "app.web.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
