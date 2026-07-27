"""The Dockerfile lets `--db`, `--config-dir`, `--host` and `--port` be
passed as trailing arguments to `docker run`, instead of only as `-e`
environment variables. That only works because of two things holding at
once, and each is pinned here rather than left to a reading of the file:

* an `ENTRYPOINT` exists, so trailing arguments APPEND to it instead of
  replacing it wholesale (a bare `CMD` would let `docker run img --db X`
  become the whole command, with nothing left to execute it);
* the bind host moved out of the command line and into `ENV` before that
  happened. `ENTRYPOINT` makes the command line overridable, and an override
  replaces arguments -- so a `--host 0.0.0.0` left in the command line would
  be silently dropped by anyone passing `--db`, the service would bind the
  host-side default (127.0.0.1) instead, and a container's own loopback
  answers nothing `docker run -p` forwards to it. That failure prints no
  error and appears in no log: the container starts, serves, and is
  unreachable. These tests exist to keep that combination from recurring.
"""
import re
import unittest


def _read(path):
    with open(path) as fh:
        return fh.read()


def _dockerfile():
    return _read("Dockerfile")


def _instruction_lines(body):
    """Non-comment lines only, so a comment mentioning `--host` in prose (as
    the migration note above ENV CRONICLED_HOST does) cannot stand in for the
    instruction itself."""
    return "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#"))


class EntrypointExists(unittest.TestCase):
    def test_the_image_declares_an_entrypoint_running_the_module(self):
        body = _instruction_lines(_dockerfile())
        self.assertRegex(
            body,
            re.compile(r'^ENTRYPOINT\s*\["python",\s*"-m",\s*"cronicled"\]\s*$', re.M))

    def test_there_is_no_plain_cmd_serving_the_inbox(self):
        # A bare CMD (instead of ENTRYPOINT) is exactly the shape that makes
        # trailing `docker run` arguments replace the whole command rather
        # than append to it.
        body = _instruction_lines(_dockerfile())
        self.assertNotRegex(body, re.compile(r'^CMD\s*\[.*cronicled', re.M))


class HostLivesInEnvNotArguments(unittest.TestCase):
    def test_env_sets_the_container_bind_host(self):
        body = _instruction_lines(_dockerfile())
        self.assertRegex(body, re.compile(r'^ENV CRONICLED_HOST=0\.0\.0\.0\s*$', re.M))

    def test_the_entrypoint_carries_no_host_argument(self):
        # The trap this whole task exists to prevent: `--host 0.0.0.0` back
        # in the argument list means an override (e.g. `--db`) DROPS it,
        # silently reverting the container to the host-side default.
        body = _instruction_lines(_dockerfile())
        m = re.search(r'^ENTRYPOINT\s*\[(.*)\]\s*$', body, re.M)
        self.assertIsNotNone(m, "no ENTRYPOINT instruction found")
        self.assertNotIn("--host", m.group(1))
        self.assertNotIn("0.0.0.0", m.group(1))


if __name__ == "__main__":
    unittest.main()
