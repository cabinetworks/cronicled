# The service ships as a container so the host's Python does not constrain it.
# PYTHON_VERSION defaults to the value in .python-version; CI passes it explicitly
# so the two can never drift.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# The registry listing is populated from these, not typed into a web form, so
# it cannot drift from the image it describes. The description is the important
# one: a published container reads as a runnable thing, and this one is not.
# Whoever finds it on the registry has none of the README's context, so the
# first sentence has to say what it is and the second what it does not do.
LABEL org.opencontainers.image.title="cronicled" \
      org.opencontainers.image.source="https://github.com/cabinetworks/cronicled" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="A pinned, reproducible runtime for a library that has no entry point yet. There is no service in this image: its default command imports the package, exercises a handful of pure functions, prints one line and exits. It is a way to run this project's code on the interpreter the project declares, not a way to run the tool."

# One runtime dependency, pinned exactly so the image stays a function of its
# inputs rather than of the day it was built. An unpinned install here would
# quietly end this image's reproducibility.
RUN pip install --no-cache-dir "jinja2==3.1.4"

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
