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
# A read-only library mount for metadata enrichment will be added here once the
# code path that uses it exists; declaring it ahead of that would be speculative.
VOLUME ["/config", "/var/lib/cronicled"]

ENV CRONICLED_CONFIG_DIR=/config

# The service entry point arrives with the service itself; until then the
# image's default command is a self-check that imports every module in the
# package and exercises a handful of pure functions end to end, so the pinned
# interpreter is proven to actually run this project's code rather than just
# having a directory copied into it (see cronicled/selfcheck.py).
CMD ["python", "-m", "cronicled.selfcheck"]
