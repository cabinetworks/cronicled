import json
import os
import tempfile
import unittest

from cronicled.config import load_server


class LoadServer(unittest.TestCase):
    def test_environment_wins_over_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            got = load_server(p, env={"STASH_URL": "http://env.example.test",
                                      "STASH_API_KEY": "E"})
            self.assertEqual(got["url"], "http://env.example.test")
            self.assertEqual(got["api_key"], "E")

    def test_falls_back_to_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test", "api_key": "F"}, fh)
            self.assertEqual(load_server(p, env={})["api_key"], "F")

    def test_missing_api_key_names_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "server.json")
            with open(p, "w") as fh:
                json.dump({"url": "http://file.example.test"}, fh)
            with self.assertRaises(ValueError) as ctx:
                load_server(p, env={})
            self.assertIn("api_key", str(ctx.exception))

    def test_absent_file_and_empty_env_raises(self):
        with self.assertRaises(ValueError):
            load_server("/nonexistent/server.json", env={})

    def test_no_default_host_is_baked_in(self):
        # a hardcoded hostname would identify the operator's machine
        import inspect
        import cronicled.config as mod
        self.assertNotIn(".local", inspect.getsource(mod))
