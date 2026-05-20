import json
import sys
import time
import types
import unittest
from unittest.mock import patch

try:
    import psutil  # noqa: F401
except ModuleNotFoundError:
    fake_psutil = types.ModuleType("psutil")
    fake_psutil.net_io_counters = lambda: types.SimpleNamespace(bytes_recv=0, bytes_sent=0)
    fake_psutil.disk_io_counters = lambda: types.SimpleNamespace(read_bytes=0, write_bytes=0)
    fake_psutil.cpu_percent = lambda interval=None: 0
    sys.modules["psutil"] = fake_psutil

try:
    import flask  # noqa: F401
except ModuleNotFoundError:
    fake_flask = types.ModuleType("flask")

    class FakeFlask:
        def __init__(self, *args, **kwargs):
            self.config = {}

        def after_request(self, fn):
            return fn

        def before_request(self, fn):
            return fn

        def get(self, *args, **kwargs):
            return lambda fn: fn

        def post(self, *args, **kwargs):
            return lambda fn: fn

        def delete(self, *args, **kwargs):
            return lambda fn: fn

        def run(self, *args, **kwargs):
            return None

    fake_flask.Flask = FakeFlask
    fake_flask.Response = lambda *args, **kwargs: None
    fake_flask.jsonify = lambda value=None, *args, **kwargs: value
    fake_flask.request = types.SimpleNamespace(args={}, remote_addr="local", get_json=lambda *args, **kwargs: {})
    fake_flask.send_from_directory = lambda *args, **kwargs: None
    fake_flask.session = {}
    sys.modules["flask"] = fake_flask

from server import app as appmod


class Pi5CodexTerminalTests(unittest.TestCase):
    def test_build_pi5_codex_command_uses_noninteractive_ssh_and_codex(self):
        with (
            patch.object(appmod, "PI5_CODEX_HOST", "192.168.0.94"),
            patch.object(appmod, "PI5_CODEX_USER", "pi5"),
            patch.object(appmod, "PI5_CODEX_WORKDIR", "/home/pi5"),
            patch.object(appmod, "PI5_CODEX_BINARY", "/home/pi5/.npm-global/bin/codex"),
            patch.object(appmod.shutil, "which", return_value="/usr/bin/ssh"),
        ):
            command = appmod.build_pi5_codex_ssh_command()

        self.assertEqual(command[0], "/usr/bin/ssh")
        self.assertIn("-tt", command)
        self.assertIn("BatchMode=yes", command)
        self.assertIn("PasswordAuthentication=no", command)
        self.assertIn("pi5@192.168.0.94", command)
        self.assertEqual(command[-1], "cd /home/pi5 && exec /home/pi5/.npm-global/bin/codex")

    def test_terminal_size_is_clamped_and_defaults_invalid_values(self):
        self.assertEqual(appmod.normalize_terminal_size(1, 999), (10, 240))
        self.assertEqual(appmod.normalize_terminal_size(120, 10), (80, 40))
        self.assertEqual(appmod.normalize_terminal_size("bad", None), (28, 110))

    def test_sse_event_encodes_json_payload(self):
        event = appmod.sse_event("output", {"text": "hello\nworld"})
        self.assertTrue(event.startswith("event: output\n"))
        self.assertTrue(event.endswith("\n\n"))
        data_line = next(line for line in event.splitlines() if line.startswith("data: "))
        self.assertEqual(json.loads(data_line.removeprefix("data: ")), {"text": "hello\nworld"})

    def test_terminal_manager_cleanup_closes_stale_sessions(self):
        class FakeSession:
            id = "abc123def456"
            client_key = "pi4:local"
            closed = False
            last_touched = time.monotonic() - 60
            close_reason = ""

            def is_stale(self, now, ttl_seconds):
                return True

            def close(self, reason):
                self.closed = True
                self.close_reason = reason

        session = FakeSession()
        manager = appmod.Pi5CodexTerminalManager(ttl_seconds=1)
        manager.sessions[session.id] = session
        manager.by_client[session.client_key] = session.id

        manager.cleanup_stale()

        self.assertNotIn(session.id, manager.sessions)
        self.assertNotIn(session.client_key, manager.by_client)
        self.assertTrue(session.closed)
        self.assertEqual(session.close_reason, "stale")

    def test_terminal_manager_create_reuses_active_session_for_client(self):
        class FakeSession:
            id = "abc123def456"
            client_key = "pi4:local"
            closed = False
            last_touched = time.monotonic()

            def __init__(self):
                self.resized_to = None
                self.touches = 0

            def is_stale(self, now, ttl_seconds):
                return False

            def resize(self, rows, cols):
                self.resized_to = (rows, cols)

            def touch(self):
                self.touches += 1

            def public(self):
                return {"id": self.id, "status": "connected"}

        session = FakeSession()
        manager = appmod.Pi5CodexTerminalManager(ttl_seconds=3600)
        manager.sessions[session.id] = session
        manager.by_client[session.client_key] = session.id

        result = manager.create(session.client_key, 32, 120)

        self.assertEqual(result, {"id": session.id, "status": "connected"})
        self.assertEqual(session.resized_to, (32, 120))
        self.assertEqual(session.touches, 1)
        self.assertEqual(manager.sessions, {session.id: session})
        self.assertEqual(manager.by_client, {session.client_key: session.id})


if __name__ == "__main__":
    unittest.main()
