# The service ships as a container so the host's Python does not constrain it.
# PYTHON_VERSION defaults to the value in .python-version; CI passes it explicitly
# so the two can never drift.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim

# The registry listing is populated from these, not typed into a web form, so
# it cannot drift from the image it describes. The description is the important
# one: a published container reads as a runnable thing, and now it is one --
# whoever finds it on the registry has none of the README's context, so this
# has to say what the default command does, what stands between its page and
# the network, and what still does not work rather than let a stranger assume.
LABEL org.opencontainers.image.title="cronicled" \
      org.opencontainers.image.source="https://github.com/cabinetworks/cronicled" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="A pinned, reproducible runtime for a media-library companion tool. The default command now starts its inbox: an HTTP page for reviewing and applying proposed library changes. It binds 0.0.0.0 inside the container (127.0.0.1 would answer nothing docker run -p forwards to it) and has NO authentication of its own -- publish the port to loopback only (-p 127.0.0.1:8571:8571), never to every interface, or this unauthenticated page becomes reachable from anywhere that can reach the host. Nothing here scans a library or writes a proposal yet; the inbox only shows what a scan run elsewhere already put in the store. The self-check that used to be this image's only default (import the package, exercise a handful of pure functions, print one line, exit) is still reachable explicitly: python -m cronicled.selfcheck."

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
# --db's own default is a relative path ("cronicled.sqlite3"), which under
# WORKDIR /app would land inside the image's writable layer -- not either
# directory declared as a volume above -- so the database would vanish with
# the container instead of surviving it. Pointing the default into the
# declared volume is what makes "docker run" without --db keep its data.
ENV CRONICLED_DB=/var/lib/cronicled/cronicled.sqlite3

# Without this the startup warnings are INVISIBLE in `docker logs`. Python
# block-buffers stdout when it is not a terminal, and a container's stdout is
# a pipe, so the binding warning -- the one telling an operator that what
# protects this unauthenticated page is their `-p` flag and not the bind
# address -- sits in a buffer that a long-running server never fills or
# flushes. Verified: without it `docker logs` returned nothing at all; with
# it, all three startup lines appear. container.md states that the warning
# prints on every container start, and this is what makes that true.
ENV PYTHONUNBUFFERED=1

# The inbox's default port (see DEFAULT_PORT in cronicled/web/app.py).
# EXPOSE is documentation only -- it does not publish anything by itself.
# `-p` at `docker run` does that, and container.md documents keeping it on
# loopback.
EXPOSE 8571

# The service entry point now exists (cronicled/__main__.py) and this is how
# the container runs it. Bound to 0.0.0.0, not the host-side default of
# 127.0.0.1: a container's own loopback answers nothing that arrives through
# `docker run -p`, which forwards to the container's other interface, so a
# service bound to 127.0.0.1 in here would build, start, and be unreachable
# from outside it. That moves the actual protection from the bind host to
# the operator's -p flag -- see the warning `serve()` prints, and
# docs/container.md, for what that flag has to do. DEFAULT_HOST (127.0.0.1)
# is untouched for anyone running this outside a container.
#
# The self-check that used to be this image's only default (see
# cronicled/selfcheck.py) is still reachable by naming it explicitly:
#   docker run --rm cronicled python -m cronicled.selfcheck
CMD ["python", "-m", "cronicled", "--host", "0.0.0.0"]
