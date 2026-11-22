FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORMFILL_MAPPINGS=/app/samples/mappings \
    FORMFILL_DATA=/app/samples/clients/clients.json \
    FORMFILL_WORKDIR=/app/work

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY samples ./samples

# Run as an unprivileged user. Mount /app/work as a volume to keep packages and the audit log.
RUN useradd --create-home --uid 10001 formfill \
    && mkdir -p /app/work \
    && chown formfill /app/work
USER formfill
VOLUME ["/app/work"]

EXPOSE 8000
# 0.0.0.0 is needed inside the container. Publish the port only to localhost or to an
# authenticating reverse proxy; see "Deployment notes" in the README.
CMD ["formfill", "serve", "--host", "0.0.0.0", "--port", "8000"]
