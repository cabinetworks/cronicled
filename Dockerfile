# The service ships as a container so the host's Python does not constrain it.
# PYTHON_VERSION defaults to the value in .python-version; CI passes it explicitly
# so the two can never drift.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# No runtime dependencies by design: the standard library only. There is
# deliberately no pip install step here.
WORKDIR /app
COPY cronicled/ ./cronicled/

# Configuration and state are mounted, never baked in: the image must contain
# nothing specific to any one installation.
#   /config  server + adapter configuration
#   /var/lib/cronicled  the database
#   /media   the library, read-only, only needed for metadata enrichment
VOLUME ["/config", "/var/lib/cronicled"]

ENV CRONICLED_CONFIG_DIR=/config

# The service entry point arrives with the service itself; until then the image
# is exercised by running the test suite against it.
CMD ["python", "-c", "import cronicled; print('cronicled runtime ready')"]
