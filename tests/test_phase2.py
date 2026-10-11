import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
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


class TestChecks(FakeProc):
    def test_inode(self):
        st = mock.Mock(f_files=1000, f_ffree=100)
        with mock.patch("os.statvfs", return_value=st, create=True):
            r = monitor.check_inode({"threshold": 85, "mounts": ["/"]})[0]
        self.assertEqual(r.key, "inode:/")
        self.assertTrue(r.bad)  # 90% used
        self.assertIn("90.0%", r.detail)

    def test_inode_skips_filesystems_without_inode_table(self):
        st = mock.Mock(f_files=0, f_ffree=0)
        with mock.patch("os.statvfs", return_value=st, create=True):
            self.assertEqual(monitor.check_inode({"threshold": 85, "mounts": ["/"]}), [])

    def test_inode_unreadable_mount_is_bad(self):
        with mock.patch("os.statvfs", side_effect=FileNotFoundError("gone"), create=True):
            self.assertTrue(monitor.check_inode({"threshold": 85, "mounts": ["/x"]})[0].bad)

    def test_ping(self):
        def fake_run(cmd, **kw):
            self.assertEqual(cmd[:5], ["ping", "-c", "1", "-W", "3"])
            self.assertEqual(cmd[-2], "--")  # host always follows "--"
            return subprocess.CompletedProcess(cmd, 0 if cmd[-1] == "up.example" else 1)

        with mock.patch("subprocess.run", fake_run):
            readings = monitor.check_ping({"targets": ["up.example", "down.example"], "timeout": 3})
        self.assertEqual([r.bad for r in readings], [False, True])
        self.assertEqual(readings[1].detail, "no reply")

    def test_ping_timeout_is_bad_and_missing_binary_is_check_error(self):
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ping", 8)):
            self.assertTrue(monitor.check_ping({"targets": ["h"], "timeout": 3})[0].bad)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            with self.assertRaises(monitor.CheckError):
                monitor.check_ping({"targets": ["h"], "timeout": 3})

    def test_ping_permission_error_is_a_check_error_not_a_false_alarm(self):
        denied = subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"ping: socket: Operation not permitted\n")
        with mock.patch("subprocess.run", return_value=denied):
            with self.assertRaises(monitor.CheckError):
                monitor.check_ping({"targets": ["h"], "timeout": 3})

    def test_ping_unknown_host_is_still_a_real_failure(self):
        unknown = subprocess.CompletedProcess([], 2, stdout=b"", stderr=b"ping: nope.example: Name or service not known\n")
        with mock.patch("subprocess.run", return_value=unknown):
            self.assertTrue(monitor.check_ping({"targets": ["nope.example"], "timeout": 3})[0].bad)

    def test_temperature(self):
        sysdir = self.proc / "sys"
        for name, kind, milli in (("thermal_zone0", "x86_pkg_temp", "85000"), ("thermal_zone1", "acpitz", "40000")):
            zone = sysdir / "class" / "thermal" / name
            zone.mkdir(parents=True)
            (zone / "temp").write_text(milli + "\n")
            (zone / "type").write_text(kind + "\n")
        with mock.patch.object(monitor, "SYS", sysdir):
            readings = monitor.check_temperature({"threshold": 80})
        self.assertEqual([r.bad for r in readings], [True, False])
        self.assertEqual(readings[0].label, "Temperature x86_pkg_temp")
        self.assertIn("85.0°C", readings[0].detail)

    def test_temperature_without_sensors_is_empty(self):
        with mock.patch.object(monitor, "SYS", self.proc / "nothing-here"):
            self.assertEqual(monitor.check_temperature({"threshold": 80}), [])

    def test_reboot_within_window_is_an_event(self):
        self.write("uptime", "125.50 400.00\n")
        r = monitor.check_reboot({"window": 600})[0]
        self.assertTrue(r.bad)
        self.assertTrue(r.event)
        self.assertIn("2m ago", r.detail)
        self.write("uptime", "90000.00 400.00\n")
        self.assertFalse(monitor.check_reboot({"window": 600})[0].bad)

    def test_reboot_without_proc_uptime_is_check_error(self):
        with self.assertRaises(monitor.CheckError):
            monitor.check_reboot({"window": 600})

    def test_ssl_expiry(self):
        day = 86400
        cfg = {"threshold": 14, "targets": ["a.example"], "timeout": 5}
        with mock.patch.object(monitor, "_cert_expiry", return_value=time.time() + 10 * day):
            r = monitor.check_ssl(cfg)[0]
        self.assertTrue(r.bad)
        self.assertEqual(r.key, "ssl:a.example")
        with mock.patch.object(monitor, "_cert_expiry", return_value=time.time() + 40 * day):
            self.assertFalse(monitor.check_ssl(cfg)[0].bad)
        with mock.patch.object(monitor, "_cert_expiry", return_value=time.time() - 3 * day):
            r = monitor.check_ssl(cfg)[0]
        self.assertTrue(r.bad)
        self.assertIn("EXPIRED 3 days ago", r.detail)

    def test_ssl_port_parsing_and_errors(self):
        calls = []

        def fake(host, port, timeout):
            calls.append((host, port))
            raise ssl.SSLCertVerificationError("certificate has expired")

        with mock.patch.object(monitor, "_cert_expiry", fake):
            r = monitor.check_ssl({"threshold": 14, "targets": ["mail.example:993", "b.example"], "timeout": 5})
        self.assertEqual(calls, [("mail.example", 993), ("b.example", 443)])
        self.assertTrue(all(x.bad for x in r))
        self.assertIn("cannot verify certificate", r[0].detail)

    def test_run_cycle_returns_readings_and_failures(self):
        self.write("meminfo", "MemTotal: 1000 kB\nMemFree: 0 kB\nMemAvailable: 500 kB\n")
        base = {"enabled": True, "duration": 0}
        settings = {
            "global": {},
            "alerts": {
                "memory": {**monitor.DEFAULTS["memory"], **base},
                "load": {**monitor.DEFAULTS["load"], **base},  # no loadavg file: crashes
            },
        }
        engine = AlertEngine("srv", lambda t: True, {})
        with self.assertLogs("monitor", level="ERROR"):
            readings, failed = monitor.run_cycle(settings, engine, now=0.0)
        self.assertEqual([r.key for r in readings], ["memory"])
        self.assertEqual(failed, ["load"])


class TestSettings(unittest.TestCase):
    def _settings(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / "settings.toml"
        p.write_text(text)
        return monitor.load_settings(p)

    def test_defaults_for_new_sections(self):
        s = self._settings("")
        self.assertFalse(s["summary"]["enabled"])
        self.assertEqual(s["summary"]["times"], ["08:00"])
        self.assertFalse(s["commands"]["enabled"])

    def test_new_alert_defaults(self):
        s = self._settings("[alerts.reboot]\n[alerts.ssl]\ntargets = ['a.example']\n")
        self.assertEqual(s["alerts"]["reboot"]["window"], 600)
        self.assertEqual(s["alerts"]["ssl"]["threshold"], 14)

    def test_bad_ping_target_rejected(self):
        for bad in ("-c 100", "-f", "a b", ""):
            with self.assertRaises(monitor.ConfigError, msg=bad):
                self._settings(f'[alerts.ping]\ntargets = ["{bad}"]\n')

    def test_bad_ssl_target_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            self._settings('[alerts.ssl]\ntargets = ["https://a.example"]\n')

    def test_bad_summary_times_rejected(self):
        for bad in ('["8:00"]', '["25:00"]', '"08:00"', '["08:60"]'):
            with self.assertRaises(monitor.ConfigError, msg=bad):
                self._settings(f"[summary]\ntimes = {bad}\n")

    def test_unknown_section_rejected(self):
        with self.assertRaises(monitor.ConfigError):
            self._settings("[sumary]\nenabled = true\n")


class TestEvents(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.engine = AlertEngine("srv", lambda t: self.sent.append(t) or True, {})

    def event(self, bad, now):
        self.engine.update(Reading("reboot", "Reboot", bad, "restarted", event=True), 0, 0, now)

    def test_event_notifies_once_never_reminds_and_recovers_silently(self):
        self.event(True, 0)
        self.event(True, 100000)
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("🔄"))
        self.assertNotIn("ongoing", self.sent[0])
        self.event(False, 100060)
        self.assertEqual(len(self.sent), 1)  # no recovery message
        self.event(True, 200000)  # a second reboot notifies again
        self.assertEqual(len(self.sent), 2)

    def test_prune_keeps_underscore_keys(self):
        self.engine.state["_summary"] = {"last_sent": 1.0}
        self.engine.prune(enabled=set(), ok_alerts=set())
        self.assertIn("_summary", self.engine.state)


class TestReportAndSummary(unittest.TestCase):
    def local(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi).timestamp()

    def test_format_duration_days(self):
        self.assertEqual(monitor.format_duration(86400), "1d")
        self.assertEqual(monitor.format_duration(3 * 86400 + 4 * 3600 + 60), "3d 4h")

    def test_format_report(self):
        readings = [Reading("cpu", "CPU", False, "usage 5%"), Reading("disk:/", "Disk /", True, "91% used")]
        text = monitor.format_report("srv", "Status", readings, ["service"], uptime=90000, age=30)
        lines = text.splitlines()
        self.assertEqual(lines[0], "📊 [srv] Status")
        self.assertIn("Uptime 1d 1h", lines[1])
        self.assertIn("1 problem, 1 OK, 1 check could not run", lines[1])
        self.assertEqual(lines[2], "Data is 30s old.")
        self.assertEqual(lines[3], "⚠️ Disk /: 91% used")  # problems come first
        self.assertEqual(lines[4], "❓ service check could not run")
        self.assertEqual(lines[5], "✅ CPU: usage 5%")

    def test_slot_due_logic(self):
        times = ["08:00", "20:00"]
        last = self.local(2026, 10, 10, 20, 5)
        self.assertIsNone(monitor.summary_slot_due(times, last, self.local(2026, 10, 11, 7, 59)))
        self.assertIsNotNone(monitor.summary_slot_due(times, last, self.local(2026, 10, 11, 8, 0)))
        last = self.local(2026, 10, 11, 8, 0)
        self.assertIsNone(monitor.summary_slot_due(times, last, self.local(2026, 10, 11, 19, 59)))
        self.assertIsNotNone(monitor.summary_slot_due(times, last, self.local(2026, 10, 11, 20, 1)))

    def _settings(self, enabled=True):
        return {"global": {"server_name": "srv"}, "summary": {"enabled": enabled, "times": ["08:00"]}}

    def test_first_run_does_not_send_then_sends_at_slot(self):
        sent, state = [], {}
        notify = lambda t: sent.append(t) or True  # noqa: E731
        readings = [Reading("cpu", "CPU", False, "ok")]
        t0 = self.local(2026, 10, 11, 9, 0)
        monitor.send_summary_if_due(self._settings(), state, notify, readings, [], t0)
        self.assertEqual(sent, [])  # the 08:00 slot passed before the first run; wait for the next one
        monitor.send_summary_if_due(self._settings(), state, notify, readings, [], t0 + 3600)
        self.assertEqual(sent, [])
        monitor.send_summary_if_due(self._settings(), state, notify, readings, [], self.local(2026, 10, 12, 8, 0))
        self.assertEqual(len(sent), 1)
        self.assertIn("Summary", sent[0])
        monitor.send_summary_if_due(self._settings(), state, notify, readings, [], self.local(2026, 10, 12, 8, 1))
        self.assertEqual(len(sent), 1)  # not sent twice

    def test_failed_summary_is_retried(self):
        state = {"_summary": {"last_sent": self.local(2026, 10, 10, 9, 0)}}
        deliver = {"ok": False}
        sent = []

        def notify(t):
            if deliver["ok"]:
                sent.append(t)
            return deliver["ok"]

        now = self.local(2026, 10, 11, 8, 0)
        monitor.send_summary_if_due(self._settings(), state, notify, [], [], now)
        deliver["ok"] = True
        monitor.send_summary_if_due(self._settings(), state, notify, [], [], now + 60)
        self.assertEqual(len(sent), 1)

    def test_disabled_summary_sends_nothing(self):
        state = {}
        monitor.send_summary_if_due(self._settings(False), state, lambda t: 1 / 0, [], [], 0.0)
        self.assertEqual(state, {})


class TestCommands(unittest.TestCase):
    def setUp(self):
        self.tg = mock.Mock()
        self.tg.chat_ids = ["111", "-222"]
        self.snapshot = monitor.Snapshot()
        self.listener = monitor.CommandListener(self.tg, "srv", self.snapshot, threading.Event())

    def msg(self, chat_id, text):
        return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}

    def test_status_before_first_cycle(self):
        self.listener.handle_update(self.msg(111, "/status"))
        self.tg.send_to.assert_called_once()
        self.assertIn("No data yet", self.tg.send_to.call_args.args[1])

    def test_status_reports_snapshot(self):
        self.snapshot.set([Reading("cpu", "CPU", False, "usage 5%")], [], time.time())
        self.listener.handle_update(self.msg(111, "/status"))
        chat, text = self.tg.send_to.call_args.args
        self.assertEqual(chat, "111")
        self.assertIn("✅ CPU: usage 5%", text)

    def test_group_chat_and_bot_suffix(self):
        self.listener.handle_update(self.msg(-222, "/Status@my_bot"))
        self.assertEqual(self.tg.send_to.call_args.args[0], "-222")

    def test_unauthorised_chat_is_ignored(self):
        with self.assertLogs("monitor", level="WARNING"):
            self.listener.handle_update(self.msg(999, "/status"))
        self.tg.send_to.assert_not_called()

    def test_non_commands_and_odd_updates_are_ignored(self):
        self.listener.handle_update(self.msg(111, "hello there"))
        self.listener.handle_update({"update_id": 2})
        self.listener.handle_update({"update_id": 3, "message": {"chat": {"id": 111}}})
        self.tg.send_to.assert_not_called()

    def test_help(self):
        self.listener.handle_update(self.msg(111, "/start"))
        self.assertIn("/status", self.tg.send_to.call_args.args[1])

    def test_get_updates_builds_request_and_parses(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"ok": true, "result": [{"update_id": 7}]}'
        with mock.patch("urllib.request.urlopen", return_value=resp) as op:
            updates = monitor.Telegram("tok", ["1"]).get_updates(5, 25)
        self.assertEqual(updates, [{"update_id": 7}])
        body = op.call_args.args[0].data.decode()
        self.assertIn("offset=5", body)
        self.assertIn("timeout=25", body)
        self.assertEqual(op.call_args.kwargs["timeout"], 35)
        with mock.patch("urllib.request.urlopen", return_value=resp) as op:
            monitor.Telegram("tok", ["1"]).get_updates(None, 0)
        self.assertNotIn("offset", op.call_args.args[0].data.decode())


if __name__ == "__main__":
    unittest.main()
