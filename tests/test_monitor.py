import http.server
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402
from monitor import AlertEngine, Reading  # noqa: E402


class FakeProc(unittest.TestCase):
    """Base class that points monitor.PROC at a temp directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.proc = Path(self._tmp.name)
        patcher = mock.patch.object(monitor, "PROC", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, text):
        (self.proc / name).write_text(text)


class TestEnvAndSettings(unittest.TestCase):
    def test_load_env_parses_quotes_comments_and_blanks(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text('# comment\n\nTELEGRAM_BOT_TOKEN="abc:123"\nTELEGRAM_CHAT_IDS=1, 2\n')
            with mock.patch.dict(os.environ, {}, clear=True):
                env = monitor.load_env(p)
        self.assertEqual(env["TELEGRAM_BOT_TOKEN"], "abc:123")
        self.assertEqual(env["TELEGRAM_CHAT_IDS"], "1, 2")

    def test_real_environment_overrides_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".env"
            p.write_text("TELEGRAM_BOT_TOKEN=fromfile\n")
            with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "fromenv"}):
                self.assertEqual(monitor.load_env(p)["TELEGRAM_BOT_TOKEN"], "fromenv")

    def _settings(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "settings.toml"
        p.write_text(text)
        return monitor.load_settings(p)

    def test_defaults_are_merged(self):
        s = self._settings('[alerts.cpu]\nthreshold = 70\n')
        self.assertEqual(s["alerts"]["cpu"]["threshold"], 70)
        self.assertEqual(s["alerts"]["cpu"]["duration"], 300)
        self.assertTrue(s["alerts"]["cpu"]["enabled"])
        self.assertEqual(s["global"]["check_interval"], 60)

    def test_unknown_alert_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            self._settings("[alerts.cpuu]\nthreshold = 70\n")

    def test_bad_endpoint_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            self._settings('[alerts.endpoint]\ntargets = ["tcp://localhost"]\n')
        with self.assertRaises(monitor.ConfigError):
            self._settings('[alerts.endpoint]\ntargets = ["ftp://localhost"]\n')

    def test_check_interval_too_small_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            self._settings("[global]\ncheck_interval = 1\n")

    def test_shipped_settings_file_is_valid(self):
        s = monitor.load_settings(Path(__file__).resolve().parent.parent / "settings.toml")
        self.assertIn("cpu", s["alerts"])


class TestProcChecks(FakeProc):
    def test_cpu_usage(self):
        self.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")

        def advance(_seconds):
            # +50 user, +50 system, +100 idle over the sample: 50% busy
            self.write("stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")

        with mock.patch.object(monitor, "_sleep", advance):
            r = monitor.check_cpu({"threshold": 40})[0]
        self.assertTrue(r.bad)
        self.assertIn("50.0%", r.detail)
        with mock.patch.object(monitor, "_sleep", lambda s: self.write("stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")):
            self.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
            self.assertFalse(monitor.check_cpu({"threshold": 60})[0].bad)

    def test_load_per_core(self):
        self.write("loadavg", "3.00 4.00 5.00 1/100 999\n")
        with mock.patch.object(os, "cpu_count", return_value=2):
            r = monitor.check_load({"threshold": 1.5})[0]
        self.assertTrue(r.bad)  # 4.0 / 2 = 2.0 per core
        with mock.patch.object(os, "cpu_count", return_value=4):
            self.assertFalse(monitor.check_load({"threshold": 1.5})[0].bad)

    def test_memory_uses_available(self):
        self.write("meminfo", "MemTotal: 1000 kB\nMemFree: 10 kB\nMemAvailable: 100 kB\n")
        r = monitor.check_memory({"threshold": 85})[0]
        self.assertTrue(r.bad)  # 90% used
        self.assertIn("90.0%", r.detail)
        self.assertFalse(monitor.check_memory({"threshold": 95})[0].bad)

    def test_swap(self):
        self.write("meminfo", "SwapTotal: 1000 kB\nSwapFree: 100 kB\n")
        self.assertTrue(monitor.check_swap({"threshold": 80})[0].bad)
        self.write("meminfo", "SwapTotal: 0 kB\nSwapFree: 0 kB\n")
        self.assertEqual(monitor.check_swap({"threshold": 80}), [])


class TestOtherChecks(unittest.TestCase):
    def test_disk(self):
        usage = mock.Mock(used=90, free=10)
        with mock.patch("shutil.disk_usage", return_value=usage):
            r = monitor.check_disk({"threshold": 85, "mounts": ["/data"]})[0]
        self.assertEqual(r.key, "disk:/data")
        self.assertTrue(r.bad)

    def test_disk_missing_mount_is_bad(self):
        with mock.patch("shutil.disk_usage", side_effect=FileNotFoundError("gone")):
            r = monitor.check_disk({"threshold": 85, "mounts": ["/nope"]})[0]
        self.assertTrue(r.bad)
        self.assertIn("cannot read usage", r.detail)

    def test_service(self):
        def fake_run(cmd, **kw):
            state = "active" if cmd[-1] == "nginx" else "failed"
            return subprocess.CompletedProcess(cmd, 0 if state == "active" else 3, stdout=state + "\n")

        with mock.patch("subprocess.run", fake_run):
            readings = monitor.check_service({"names": ["nginx", "redis"]})
        self.assertEqual([r.bad for r in readings], [False, True])
        self.assertEqual(readings[1].detail, "status: failed")

    def test_service_timeout(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 10)):
            r = monitor.check_service({"names": ["x"]})[0]
        self.assertTrue(r.bad)

    def test_service_without_systemctl_is_a_check_error(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(monitor.CheckError):
                monitor.check_service({"names": ["x"]})

    def test_endpoint_tcp_and_http(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        tcp_port = listener.getsockname()[1]

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/ok" else 503)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        http_port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)  # cleanups run last-in first-out
        self.addCleanup(server.shutdown)

        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()  # nothing listens here now

        cfg = {
            "timeout": 2,
            "targets": [
                f"tcp://127.0.0.1:{tcp_port}",
                f"tcp://127.0.0.1:{closed_port}",
                f"http://127.0.0.1:{http_port}/ok",
                f"http://127.0.0.1:{http_port}/bad",
            ],
        }
        try:
            bad = [r.bad for r in monitor.check_endpoint(cfg)]
        finally:
            listener.close()
        self.assertEqual(bad, [False, True, False, True])


class TestTelegram(unittest.TestCase):
    def test_sends_to_every_chat(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", return_value=resp) as op:
            ok = monitor.Telegram("tok", ["1", "2"]).send("hi")
        self.assertTrue(ok)
        self.assertEqual(op.call_count, 2)
        body = op.call_args_list[1].args[0].data.decode()
        self.assertIn("chat_id=2", body)
        self.assertIn("text=hi", body)

    def test_partial_failure_still_counts_as_delivered(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        with mock.patch("urllib.request.urlopen", side_effect=[urllib.error.URLError("down"), resp]):
            with self.assertLogs("monitor", level="ERROR"):
                self.assertTrue(monitor.Telegram("tok", ["1", "2"]).send("hi"))

    def test_total_failure_returns_false(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            with self.assertLogs("monitor", level="ERROR"):
                self.assertFalse(monitor.Telegram("tok", ["1"]).send("hi"))


class TestAlertEngine(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.deliver = True
        self.engine = AlertEngine("srv", self._notify, {})

    def _notify(self, text):
        if self.deliver:
            self.sent.append(text)
        return self.deliver

    def bad(self, now, duration=300, repeat=3600):
        self.engine.update(Reading("cpu", "CPU", True, "usage 95%"), duration, repeat, now)

    def good(self, now, duration=300, repeat=3600):
        self.engine.update(Reading("cpu", "CPU", False, "usage 10%"), duration, repeat, now)

    def test_no_alert_before_duration(self):
        self.bad(0)
        self.bad(299)
        self.assertEqual(self.sent, [])

    def test_alert_after_duration_once(self):
        self.bad(0)
        self.bad(300)
        self.bad(360)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("🔴 [srv] CPU: usage 95%", self.sent[0])

    def test_spike_resets_timer(self):
        self.bad(0)
        self.good(200)
        self.bad(250)
        self.bad(500)  # only 250s since the new bad_since
        self.assertEqual(self.sent, [])
        self.bad(550)
        self.assertEqual(len(self.sent), 1)

    def test_immediate_alert_with_zero_duration(self):
        self.bad(0, duration=0)
        self.assertEqual(len(self.sent), 1)

    def test_repeat_after_interval(self):
        self.bad(0, duration=0)
        self.bad(3599, duration=0)
        self.assertEqual(len(self.sent), 1)
        self.bad(3600, duration=0)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("🔁", self.sent[1])

    def test_repeat_zero_means_never(self):
        self.bad(0, duration=0, repeat=0)
        self.bad(10**6, duration=0, repeat=0)
        self.assertEqual(len(self.sent), 1)

    def test_recovery_message(self):
        self.bad(0, duration=0)
        self.good(125)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("🟢", self.sent[1])
        self.assertIn("lasted 2m", self.sent[1])
        self.good(200)
        self.assertEqual(len(self.sent), 2)  # no second recovery

    def test_no_recovery_message_if_never_alerted(self):
        self.bad(0)
        self.good(60)
        self.assertEqual(self.sent, [])

    def test_failed_delivery_is_retried_next_cycle(self):
        self.deliver = False
        self.bad(0, duration=0)
        self.assertEqual(self.sent, [])
        self.deliver = True
        self.bad(60, duration=0)
        self.assertEqual(len(self.sent), 1)

    def test_failed_recovery_is_retried(self):
        self.bad(0, duration=0)
        self.deliver = False
        self.good(60)
        self.deliver = True
        self.good(120)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("🟢", self.sent[1])

    def test_prune(self):
        self.bad(0, duration=0)
        self.engine.prune(enabled={"cpu"}, ok_alerts={"cpu"})  # cpu key seen: kept
        self.assertIn("cpu", self.engine.state)
        self.engine.prune(enabled={"cpu"}, ok_alerts={"cpu"})  # not seen this cycle: dropped
        self.assertNotIn("cpu", self.engine.state)

    def test_prune_keeps_state_when_check_crashed_or_drops_when_disabled(self):
        self.bad(0, duration=0)
        self.engine.prune(enabled={"cpu"}, ok_alerts=set())  # check crashed
        self.assertIn("cpu", self.engine.state)
        self.engine.prune(enabled=set(), ok_alerts=set())  # disabled
        self.assertNotIn("cpu", self.engine.state)


class TestRunCycleAndState(FakeProc):
    def test_cycle_sends_alert_and_state_roundtrips(self):
        self.write("meminfo", "MemTotal: 1000 kB\nMemFree: 0 kB\nMemAvailable: 50 kB\n")
        settings = {
            "global": {},
            "alerts": {"memory": {"enabled": True, **monitor.DEFAULTS["memory"], "duration": 0}},
        }
        sent = []
        engine = AlertEngine("srv", lambda t: sent.append(t) or True, {})
        monitor.run_cycle(settings, engine, now=1000.0)
        self.assertEqual(len(sent), 1)

        path = self.proc / "state.json"
        monitor.save_state(path, engine.state)
        reloaded = monitor.load_state(path)
        self.assertTrue(reloaded["memory"]["alerting"])

        # Simulate a restart: same state, still bad, within repeat window -> no duplicate.
        engine2 = AlertEngine("srv", lambda t: sent.append(t) or True, reloaded)
        monitor.run_cycle(settings, engine2, now=1060.0)
        self.assertEqual(len(sent), 1)

    def test_crashing_check_does_not_stop_others(self):
        self.write("meminfo", "MemTotal: 1000 kB\nMemFree: 0 kB\nMemAvailable: 50 kB\n")  # no loadavg file
        base = {"enabled": True, "duration": 0}
        settings = {
            "global": {},
            "alerts": {
                "load": {**monitor.DEFAULTS["load"], **base},
                "memory": {**monitor.DEFAULTS["memory"], **base},
            },
        }
        sent = []
        engine = AlertEngine("srv", lambda t: sent.append(t) or True, {})
        with self.assertLogs("monitor", level="ERROR"):
            monitor.run_cycle(settings, engine, now=0.0)
        self.assertEqual(len(sent), 1)
        self.assertIn("Memory", sent[0])

    def test_load_state_tolerates_garbage(self):
        p = self.proc / "state.json"
        p.write_text("{not json")
        with self.assertLogs("monitor", level="WARNING"):
            self.assertEqual(monitor.load_state(p), {})
        self.assertEqual(monitor.load_state(self.proc / "missing.json"), {})


class TestFormatDuration(unittest.TestCase):
    def test_format(self):
        self.assertEqual(monitor.format_duration(45), "45s")
        self.assertEqual(monitor.format_duration(300), "5m")
        self.assertEqual(monitor.format_duration(3600), "1h")
        self.assertEqual(monitor.format_duration(4500), "1h 15m")


if __name__ == "__main__":
    unittest.main()
