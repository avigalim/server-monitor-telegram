import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402
from monitor import AlertEngine  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "checks.d"

GOOD_CHECK = """
NAME = "{name}"
DEFAULTS = {{"limit": 5, "repeat_every": 60}}

def check(cfg, api):
    return [api.Reading("{name}", "Thing", cfg["limit"] > 9, f"limit is {{cfg['limit']}}")]
"""


class Folder(unittest.TestCase):
    """A temp directory holding a settings file and a checks folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.checks = self.base / "checks.d"
        self.checks.mkdir()
        self.addCleanup(lambda: [sys.modules.pop(m) for m in list(sys.modules) if m.startswith("monitor_extra_")])

    def add(self, filename, source):
        (self.checks / filename).write_text(textwrap.dedent(source), encoding="utf-8")

    def settings(self, toml):
        path = self.base / "settings.toml"
        path.write_text(textwrap.dedent(toml), encoding="utf-8")
        return monitor.load_settings(path)


class TestLoading(Folder):
    def test_good_file_is_loaded_and_configured_like_any_alert(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        s = self.settings('[global]\nextra_checks = "checks.d"\n[alerts.thing]\nlimit = 12\n')
        cfg = s["alerts"]["thing"]
        self.assertEqual((cfg["enabled"], cfg["limit"], cfg["repeat_every"], cfg["duration"]), (True, 12, 60, 0))
        self.assertIn("thing", s["extra_checks"])
        readings, failed = monitor.run_cycle(s, AlertEngine("srv", lambda t: True, {}), now=0.0)
        self.assertEqual(failed, [])
        self.assertTrue(readings[0].bad)
        self.assertEqual(readings[0].detail, "limit is 12")

    def test_nothing_is_loaded_unless_configured(self):
        self.add("thing.py", "raise SystemExit('must never run')\n")
        s = self.settings("[alerts.cpu]\n")
        self.assertEqual(s["extra_checks"], {})

    def test_alert_section_without_the_folder_is_still_unknown(self):
        with self.assertRaises(monitor.ConfigError):
            self.settings("[alerts.thing]\nlimit = 1\n")

    def test_defaults_for_duration_and_repeat_when_the_file_gives_none(self):
        self.add("bare.py", 'NAME = "bare"\ndef check(cfg, api):\n    return []\n')
        cfg = self.settings('[global]\nextra_checks = "checks.d"\n[alerts.bare]\n')["alerts"]["bare"]
        self.assertEqual((cfg["duration"], cfg["repeat_every"]), (0, 3600))

    def test_folder_is_relative_to_the_settings_file_or_absolute(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        s = self.settings(f'[global]\nextra_checks = "{self.checks.as_posix()}"\n[alerts.thing]\n')
        self.assertIn("thing", s["extra_checks"])

    def test_missing_folder(self):
        with self.assertRaisesRegex(monitor.ConfigError, "folder not found"):
            self.settings('[global]\nextra_checks = "nope"\n')

    def test_extra_checks_must_be_a_string(self):
        with self.assertRaises(monitor.ConfigError):
            self.settings("[global]\nextra_checks = 5\n")

    def test_files_starting_with_underscore_or_not_py_are_ignored(self):
        self.add("_helper.py", "raise SystemExit('must never run')\n")
        self.add("notes.txt", "raise SystemExit('must never run')\n")
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        self.assertEqual(list(monitor.load_extra_checks(self.checks)), ["thing"])

    def test_broken_file_stops_startup_and_names_the_file(self):
        self.add("broken.py", "def check(:\n")
        with self.assertRaisesRegex(monitor.ConfigError, r"broken\.py.*SyntaxError"):
            monitor.load_extra_checks(self.checks)

    def test_file_that_raises_on_import(self):
        self.add("boom.py", 'raise RuntimeError("no luck")\n')
        with self.assertRaisesRegex(monitor.ConfigError, r"boom\.py.*RuntimeError.*no luck"):
            monitor.load_extra_checks(self.checks)

    def test_file_that_exits_on_import_is_reported_by_name(self):
        self.add("quitter.py", "import sys\nsys.exit(3)\n")
        with self.assertRaisesRegex(monitor.ConfigError, r"quitter\.py.*SystemExit"):
            monitor.load_extra_checks(self.checks)

    def test_bad_names(self):
        for bad in ('"Upper"', '"has space"', '"9digit"', '"a:b"', "None", "5"):
            with self.subTest(name=bad):
                self.add("x.py", f"NAME = {bad}\ndef check(cfg, api):\n    return []\n")
                with self.assertRaisesRegex(monitor.ConfigError, "NAME must be"):
                    monitor.load_extra_checks(self.checks)

    def test_name_clash_with_builtin(self):
        self.add("x.py", 'NAME = "cpu"\ndef check(cfg, api):\n    return []\n')
        with self.assertRaisesRegex(monitor.ConfigError, "already a built-in"):
            monitor.load_extra_checks(self.checks)

    def test_two_files_with_the_same_name(self):
        self.add("a.py", GOOD_CHECK.format(name="same"))
        self.add("b.py", GOOD_CHECK.format(name="same"))
        with self.assertRaisesRegex(monitor.ConfigError, "already used"):
            monitor.load_extra_checks(self.checks)

    def test_missing_check_function_and_bad_defaults(self):
        self.add("x.py", 'NAME = "x"\n')
        with self.assertRaisesRegex(monitor.ConfigError, "check"):
            monitor.load_extra_checks(self.checks)
        self.add("x.py", 'NAME = "x"\nDEFAULTS = [1]\ndef check(cfg, api):\n    return []\n')
        with self.assertRaisesRegex(monitor.ConfigError, "DEFAULTS must be a dict"):
            monitor.load_extra_checks(self.checks)

    def test_world_writable_folder_or_file_is_refused(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        with mock.patch.object(monitor, "_world_writable", lambda p: Path(p) == self.checks):
            with self.assertRaisesRegex(monitor.ConfigError, "writable by everyone"):
                monitor.load_extra_checks(self.checks)
        with mock.patch.object(monitor, "_world_writable", lambda p: Path(p).name == "thing.py"):
            with self.assertRaisesRegex(monitor.ConfigError, "writable by everyone"):
                monitor.load_extra_checks(self.checks)

    @unittest.skipUnless(os.name == "posix", "file modes need POSIX")
    def test_world_writable_detection_on_real_files(self):
        path = self.base / "f"
        path.write_text("x")
        os.chmod(path, 0o644)
        self.assertFalse(monitor._world_writable(path))
        os.chmod(path, 0o666)
        self.assertTrue(monitor._world_writable(path))


class TestRunning(Folder):
    def run_one(self, source, name="thing"):
        self.add("thing.py", source)
        s = self.settings(f'[global]\nextra_checks = "checks.d"\n[alerts.{name}]\n')
        return s, monitor.run_cycle(s, AlertEngine("srv", lambda t: True, {}), now=0.0)

    def test_check_that_raises_is_skipped_and_others_still_run(self):
        self.add("bad.py", 'NAME = "bad"\ndef check(cfg, api):\n    raise ValueError("oops")\n')
        self.add("good.py", GOOD_CHECK.format(name="good"))
        s = self.settings('[global]\nextra_checks = "checks.d"\n[alerts.bad]\n[alerts.good]\n')
        with self.assertLogs("monitor", level="ERROR"):
            readings, failed = monitor.run_cycle(s, AlertEngine("srv", lambda t: True, {}), now=0.0)
        self.assertEqual(failed, ["bad"])
        self.assertEqual([r.key for r in readings], ["good"])

    def test_check_error_is_a_one_line_skip(self):
        source = 'NAME = "thing"\ndef check(cfg, api):\n    raise api.CheckError("tool missing")\n'
        with self.assertLogs("monitor", level="ERROR") as logs:
            _, (readings, failed) = self.run_one(source)
        self.assertEqual((readings, failed), ([], ["thing"]))
        self.assertIn("tool missing", logs.output[0])
        self.assertNotIn("Traceback", logs.output[0])

    def test_wrong_return_type_is_rejected(self):
        for bad in ("None", '"text"', "[1, 2]", '{"key": "thing"}'):
            with self.subTest(returns=bad):
                source = f'NAME = "thing"\ndef check(cfg, api):\n    return {bad}\n'
                with self.assertLogs("monitor", level="ERROR"):
                    _, (readings, failed) = self.run_one(source)
                self.assertEqual((readings, failed), ([], ["thing"]))

    def test_reading_keys_must_stay_in_the_checks_own_namespace(self):
        for key in ("cpu", "other", "thingy", "other:thing"):
            with self.subTest(key=key):
                source = f'NAME = "thing"\ndef check(cfg, api):\n    return [api.Reading("{key}", "L", True, "d")]\n'
                with self.assertLogs("monitor", level="ERROR"):
                    _, (readings, failed) = self.run_one(source)
                self.assertEqual(failed, ["thing"])

    def test_several_keys_with_the_checks_prefix_work(self):
        source = ('NAME = "thing"\ndef check(cfg, api):\n'
                  '    return [api.Reading("thing", "A", False, "ok"), api.Reading("thing:b", "B", True, "no")]\n')
        _, (readings, failed) = self.run_one(source)
        self.assertEqual([r.key for r in readings], ["thing", "thing:b"])

    def test_alert_reminder_and_recovery_use_the_alert_engine(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        s = self.settings('[global]\nextra_checks = "checks.d"\n[alerts.thing]\nlimit = 12\nrepeat_every = 100\n')
        sent = []
        engine = AlertEngine("srv", lambda t: sent.append(t) or True, {})
        monitor.run_cycle(s, engine, now=0.0)
        monitor.run_cycle(s, engine, now=50.0)
        monitor.run_cycle(s, engine, now=100.0)
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].startswith("🔴 [srv] Thing: limit is 12"))
        self.assertTrue(sent[1].startswith("🔁"))
        s["alerts"]["thing"]["limit"] = 1
        monitor.run_cycle(s, engine, now=160.0)
        self.assertTrue(sent[2].startswith("🟢"))

    def test_state_for_a_disabled_extra_check_is_pruned(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        s = self.settings('[global]\nextra_checks = "checks.d"\n[alerts.thing]\nlimit = 12\n[alerts.cpu]\nenabled = false\n')
        engine = AlertEngine("srv", lambda t: True, {})
        monitor.run_cycle(s, engine, now=0.0)
        self.assertIn("thing", engine.state)
        s["alerts"]["thing"]["enabled"] = False
        monitor.run_cycle(s, engine, now=60.0)
        self.assertNotIn("thing", engine.state)

    def test_agent_payload_carries_extra_readings_and_policy_and_central_reads_them(self):
        self.add("thing.py", GOOD_CHECK.format(name="thing"))
        s = self.settings('[global]\nmode = "agent"\nextra_checks = "checks.d"\n'
                          '[alerts.thing]\nlimit = 12\nduration = 30\nrepeat_every = 90\n')
        _, results, failed = monitor.collect_readings(s)
        snapshot = monitor.Snapshot()
        snapshot.set([r for _, rs in results for r in rs], failed, time.time())
        payload = monitor.build_agent_payload(s, snapshot)
        self.assertEqual(payload["policy"]["thing"], {"duration": 30, "repeat_every": 90})
        import json
        parsed = monitor.parse_agent_payload(json.dumps(payload))
        self.assertEqual([r.key for r in parsed["readings"]], ["thing"])
        self.assertEqual(parsed["policy"]["thing"]["repeat_every"], 90.0)


class TestShippedExample(Folder):
    def test_example_loads_and_reports_a_fresh_file_as_ok_and_an_old_one_as_bad(self):
        extra = monitor.load_extra_checks(EXAMPLES)
        defaults, check = extra["backup_age"]
        backup = self.base / "db.sql.gz"
        backup.write_text("x")
        cfg = {**defaults, "file": str(backup), "max_hours": 26}
        self.assertFalse(check(cfg)[0].bad)
        old = time.time() - 30 * 3600
        os.utime(backup, (old, old))
        r = check(cfg)[0]
        self.assertTrue(r.bad)
        self.assertIn("1d 6h old (limit 26h)", r.detail)
        missing = check({**cfg, "file": str(self.base / "nope")})[0]
        self.assertTrue(missing.bad)
        self.assertIn("cannot read", missing.detail)


if __name__ == "__main__":
    unittest.main()
