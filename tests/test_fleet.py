import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import monitor  # noqa: E402
from monitor import AgentError, AlertEngine, Reading  # noqa: E402

TOKEN = "t" * 20


def write_settings(text):
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "settings.toml"
    path.write_text(text, encoding="utf-8")
    return tmp, path


CENTRAL = """
[global]
mode = "central"
[[servers]]
name = "web-01"
url = "http://10.0.0.1:8765"
[[servers]]
name = "db-01"
url = "https://10.0.0.2:8765"
token_env = "AGENT_TOKEN_DB"
"""


class TestSettings(unittest.TestCase):
    def load(self, text):
        tmp, path = write_settings(text)
        self.addCleanup(tmp.cleanup)
        return monitor.load_settings(path)

    def test_default_mode_is_local(self):
        s = self.load("[global]\nserver_name='x'\n")
        self.assertEqual(s["global"]["mode"], "local")
        self.assertEqual(s["servers"], [])

    def test_unknown_mode(self):
        with self.assertRaises(monitor.ConfigError):
            self.load("[global]\nmode='boss'\n")

    def test_central_servers(self):
        s = self.load(CENTRAL)
        self.assertEqual([x["name"] for x in s["servers"]], ["web-01", "db-01"])
        self.assertEqual(s["servers"][0]["token_env"], "AGENT_TOKEN")
        self.assertEqual(s["servers"][1]["token_env"], "AGENT_TOKEN_DB")
        self.assertEqual(s["central"]["agent_duration"], 120)

    def test_central_needs_servers(self):
        with self.assertRaises(monitor.ConfigError):
            self.load("[global]\nmode='central'\n")

    def test_bad_server_entries(self):
        base = "[global]\nmode='central'\n[[servers]]\n"
        for body in (
            "name='has space'\nurl='http://h:1'\n",
            "name='a'\nurl='ftp://h'\n",
            "name='a'\nurl='nonsense'\n",
            "name='a'\nurl='http://h:1'\ntoken_env='bad name'\n",
            "name='a'\nurl='http://h:1'\n[[servers]]\nname='A'\nurl='http://h:2'\n",  # duplicate, case-insensitive
        ):
            with self.subTest(body=body), self.assertRaises(monitor.ConfigError):
                self.load(base + body)

    def test_agent_settings(self):
        s = self.load("[global]\nmode='agent'\n[agent]\nport = 9000\n")
        self.assertEqual(s["agent"]["port"], 9000)
        self.assertEqual(s["agent"]["listen"], "0.0.0.0")
        for bad in ("port = 0", "port = 70000", "port = 'x'", "tls_cert = 'a.pem'"):
            with self.subTest(bad=bad), self.assertRaises(monitor.ConfigError):
                self.load(f"[agent]\n{bad}\n")

    def test_server_tokens(self):
        s = self.load(CENTRAL)
        with self.assertRaises(monitor.ConfigError) as cm:
            monitor.server_tokens(s, {"AGENT_TOKEN": TOKEN})
        self.assertIn("AGENT_TOKEN_DB", str(cm.exception))
        tokens = monitor.server_tokens(s, {"AGENT_TOKEN": TOKEN, "AGENT_TOKEN_DB": "d" * 20})
        self.assertEqual(tokens, {"web-01": TOKEN, "db-01": "d" * 20})

    def test_server_tokens_reject_short(self):
        s = self.load(CENTRAL)
        with self.assertRaises(monitor.ConfigError):
            monitor.server_tokens(s, {"AGENT_TOKEN": "short", "AGENT_TOKEN_DB": "d" * 20})


def agent_settings():
    return {
        "global": {"server_name": "web-01", "check_interval": 60},
        "alerts": {
            "disk": {"enabled": True, "duration": 0, "repeat_every": 600},
            "cpu": {"enabled": True, "duration": 300, "repeat_every": 3600},
            "swap": {"enabled": False, "duration": 1, "repeat_every": 1},
        },
    }


def make_snapshot(readings, failed=(), taken=1000.0):
    snap = monitor.Snapshot()
    snap.set(readings, list(failed), taken)
    return snap


class TestPayload(unittest.TestCase):
    snapshot = staticmethod(make_snapshot)

    def test_none_before_first_cycle(self):
        self.assertIsNone(monitor.build_agent_payload(agent_settings(), monitor.Snapshot()))

    def test_roundtrip(self):
        readings = [Reading("disk:/", "Disk /", True, "91%"), Reading("reboot", "Reboot", True, "restarted", event=True)]
        payload = monitor.build_agent_payload(agent_settings(), self.snapshot(readings, ["ping"]), now=1030.0)
        self.assertEqual(payload["age"], 30.0)
        self.assertEqual(set(payload["policy"]), {"disk", "cpu"})  # disabled alerts are not offered
        parsed = monitor.parse_agent_payload(json.dumps(payload))
        self.assertEqual(parsed["readings"], readings)
        self.assertEqual(parsed["failed"], ["ping"])
        self.assertEqual(parsed["policy"]["disk"], {"duration": 0.0, "repeat_every": 600.0})

    def test_parse_rejects_garbage(self):
        for body in (b"not json", b"[]", b"{}", b'{"version": 99}', json.dumps({"version": 1, "readings": [{}]}).encode()):
            with self.subTest(body=body), self.assertRaises(AgentError):
                monitor.parse_agent_payload(body)


class AgentHttpCase(unittest.TestCase):
    """A real agent on a free localhost port."""

    def start(self, payload):
        holder = {"payload": payload}
        settings = {"agent": {"listen": "127.0.0.1", "port": 0, "tls_cert": "", "tls_key": ""}}
        server = monitor.make_agent_server(settings, TOKEN, lambda: holder["payload"])
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}", holder

    def get(self, url, token=TOKEN):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=5)


class TestAgentServer(AgentHttpCase):
    def test_serves_payload_with_valid_token(self):
        url, _ = self.start({"hello": "world"})
        with self.get(url + "/status") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(json.load(resp), {"hello": "world"})

    def test_wrong_or_missing_token_is_refused(self):
        url, _ = self.start({"hello": "world"})
        for token in ("wrong" * 5, None):
            with self.subTest(token=token), self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(url + "/status", token)
            self.assertEqual(cm.exception.code, 401)

    def test_token_is_checked_before_the_path(self):
        url, _ = self.start({})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url + "/nothing", None)
        self.assertEqual(cm.exception.code, 401)  # an unauthenticated caller learns nothing about paths
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url + "/nothing")
        self.assertEqual(cm.exception.code, 404)

    def test_no_data_yet_is_503(self):
        url, _ = self.start(None)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get(url + "/status")
        self.assertEqual(cm.exception.code, 503)


class TestFetchAgent(AgentHttpCase):
    def server(self, url):
        return {"name": "x", "url": url, "token_env": "AGENT_TOKEN", "ca_file": ""}

    def test_fetch_ok(self):
        payload = monitor.build_agent_payload(
            agent_settings(), make_snapshot([Reading("disk:/", "Disk /", False, "ok")]), now=1001.0
        )
        url, _ = self.start(payload)
        data = monitor.fetch_agent(self.server(url), TOKEN, 5)
        self.assertEqual(data["readings"][0].key, "disk:/")

    def test_fetch_failures_become_agent_errors(self):
        url, _ = self.start({})
        with self.assertRaisesRegex(AgentError, "refused the token"):
            monitor.fetch_agent(self.server(url), "x" * 20, 5)
        with self.assertRaisesRegex(AgentError, "invalid answer"):
            monitor.fetch_agent(self.server(url), TOKEN, 5)
        url2, _ = self.start(None)
        with self.assertRaisesRegex(AgentError, "HTTP 503"):
            monitor.fetch_agent(self.server(url2), TOKEN, 5)
        with self.assertRaisesRegex(AgentError, "unreachable"):
            monitor.fetch_agent(self.server("http://127.0.0.1:1"), TOKEN, 2)


def payload_data(readings, policy=None, failed=(), age=5.0, interval=60.0):
    names = {r.key.split(":", 1)[0] for r in readings}
    return {
        "readings": readings,
        "failed": list(failed),
        "age": age,
        "interval": interval,
        "uptime": 3600.0,
        "policy": policy if policy is not None else {n: {"duration": 0.0, "repeat_every": 0.0} for n in names},
    }


class TestCentralCycle(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "servers": [
                {"name": "web-01", "url": "http://a", "token_env": "AGENT_TOKEN", "ca_file": ""},
                {"name": "db-01", "url": "http://b", "token_env": "AGENT_TOKEN", "ca_file": ""},
            ],
            "central": {"timeout": 5, "agent_duration": 120, "agent_repeat_every": 1800},
        }
        self.sent = []
        self.states = {"web-01": {}, "db-01": {}}
        self.engines = {n: AlertEngine(n, lambda t: self.sent.append(t) or True, self.states[n]) for n in self.states}
        self.fleet = monitor.Fleet()
        self.tokens = {"web-01": TOKEN, "db-01": TOKEN}
        self.results = {}

    def fetch(self, server, token, timeout):
        result = self.results[server["name"]]
        if isinstance(result, Exception):
            raise result
        return result

    def cycle(self, now):
        monitor.central_cycle(self.settings, self.engines, self.fleet, self.tokens, now, fetch=self.fetch)

    def test_alert_is_labelled_with_the_server_and_recovers(self):
        bad, ok = Reading("disk:/", "Disk /", True, "91%"), Reading("disk:/", "Disk /", False, "40%")
        self.results = {"web-01": payload_data([bad]), "db-01": payload_data([ok])}
        self.cycle(1000.0)
        self.assertEqual(self.sent, ["🔴 [web-01] Disk /: 91%"])
        self.results["web-01"] = payload_data([ok])
        self.cycle(1100.0)
        self.assertIn("🟢 [web-01] Disk / recovered", self.sent[-1])
        self.assertEqual(len(self.sent), 2)  # db-01 never alerted

    def test_agent_duration_and_policy_come_from_the_right_place(self):
        bad = Reading("cpu", "CPU", True, "95%")
        policy = {"cpu": {"duration": 300.0, "repeat_every": 0.0}}
        self.results = {"web-01": payload_data([bad], policy), "db-01": payload_data([])}
        self.cycle(1000.0)
        self.cycle(1200.0)
        self.assertEqual(self.sent, [])  # agent said: only after 300 s
        self.cycle(1300.0)
        self.assertEqual(len(self.sent), 1)

    def test_unreachable_agent_alerts_after_duration_then_recovers(self):
        ok = payload_data([Reading("disk:/", "Disk /", False, "40%")])
        self.results = {"web-01": AgentError("agent unreachable: timed out"), "db-01": ok}
        self.cycle(1000.0)
        self.cycle(1060.0)
        self.assertEqual(self.sent, [])  # agent_duration is 120 s
        self.cycle(1120.0)
        self.assertEqual(self.sent, ["🔴 [web-01] Agent: agent unreachable: timed out\nProblem ongoing for 2m."])
        self.results["web-01"] = ok
        self.cycle(1180.0)
        self.assertIn("🟢 [web-01] Agent recovered", self.sent[-1])

    def test_unreachable_agent_keeps_other_alert_state(self):
        bad = Reading("disk:/", "Disk /", True, "91%")
        self.results = {"web-01": payload_data([bad]), "db-01": payload_data([])}
        self.cycle(1000.0)
        self.assertTrue(self.states["web-01"]["disk:/"]["alerting"])
        self.results["web-01"] = AgentError("agent unreachable: down")
        self.cycle(1060.0)
        self.assertTrue(self.states["web-01"]["disk:/"]["alerting"])  # not pruned, no false recovery
        self.assertEqual(len(self.sent), 1)

    def test_stale_data_counts_as_a_problem(self):
        data = payload_data([Reading("disk:/", "Disk /", True, "91%")], age=900.0, interval=60.0)
        self.results = {"web-01": data, "db-01": payload_data([])}
        self.cycle(1000.0)
        self.cycle(1200.0)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Agent: agent's data is 15m old", self.sent[0])
        self.assertNotIn("Disk", self.sent[0])  # old readings do not raise disk alerts

    def test_prune_forgets_alerts_the_agent_stopped_offering(self):
        bad = Reading("disk:/", "Disk /", True, "91%")
        self.results = {"web-01": payload_data([bad]), "db-01": payload_data([])}
        self.cycle(1000.0)
        self.results["web-01"] = payload_data([], policy={"cpu": {"duration": 0.0, "repeat_every": 0.0}})
        self.cycle(1060.0)
        self.assertNotIn("disk:/", self.states["web-01"])

    def test_a_crashing_fetch_does_not_stop_the_other_servers(self):
        self.results = {"web-01": ZeroDivisionError("boom"), "db-01": payload_data([Reading("disk:/", "Disk /", True, "91%")])}
        self.cycle(1000.0)
        self.assertEqual(self.sent, ["🔴 [db-01] Disk /: 91%"])
        self.assertIn("internal error", self.fleet.get("web-01")["error"])


class TestFleetReport(unittest.TestCase):
    def rows(self):
        fleet = monitor.Fleet()
        ok, bad = Reading("cpu", "CPU", False, "3%"), Reading("disk:/", "Disk /", True, "91%")
        fleet.record("web-01", payload_data([ok, bad]), None, 1000.0)
        fleet.record("db-01", payload_data([ok]), None, 1000.0)
        fleet.record("old-01", payload_data([ok]), None, 400.0)
        fleet.record("old-01", None, "agent unreachable: timed out", None)
        return fleet.rows(["web-01", "db-01", "old-01", "new-01"])

    def test_overview(self):
        text = monitor.format_fleet_report("Status", self.rows(), now=1060.0)
        lines = text.splitlines()
        self.assertEqual(lines[0], "📊 Status: 4 servers, 1 unreachable, 1 with problems.")
        self.assertIn("⚠️ web-01: 1 problem, 1 OK", lines)
        self.assertIn("   Disk /: 91%", lines)
        self.assertIn("✅ db-01: all 1 OK", lines)
        self.assertIn("🔴 old-01: agent unreachable: timed out", lines)
        self.assertIn("   Last data 11m ago: 0 problems, 1 OK.", lines)
        self.assertIn("⏳ new-01: no data yet", lines)

    def test_failed_record_keeps_the_last_good_readings(self):
        fleet = monitor.Fleet()
        fleet.record("a", payload_data([Reading("cpu", "CPU", False, "3%")]), None, 1000.0)
        fleet.record("a", None, "down", None)
        entry = fleet.get("a")
        self.assertEqual(entry["error"], "down")
        self.assertEqual(entry["taken"], 1000.0)
        self.assertEqual(len(entry["readings"]), 1)


class TestCentralCommands(unittest.TestCase):
    def setUp(self):
        self.fleet = monitor.Fleet()
        self.fleet.record("web-01", payload_data([Reading("cpu", "CPU", False, "3%")]), None, time.time() - 30)
        self.listener = monitor.CentralCommandListener(
            mock.Mock(chat_ids=["1"]), "central", ["web-01", "db-01"], self.fleet, threading.Event()
        )

    def test_status_overview(self):
        self.assertIn("📊 Status: 2 servers", self.listener.reply("/status", []))

    def test_status_for_one_server_ignores_case(self):
        text = self.listener.reply("/status", ["WEB-01"])
        self.assertIn("📊 [web-01] Status", text)
        self.assertIn("Data is 30s old", text)

    def test_unknown_server_lists_known_ones(self):
        self.assertEqual(self.listener.reply("/status", ["nope"]), "Unknown server 'nope'. Known servers: web-01, db-01")

    def test_server_without_data(self):
        self.assertIn("No data yet", self.listener.reply("/status", ["db-01"]))
        self.fleet.record("db-01", None, "agent unreachable: x", None)
        self.assertIn("agent unreachable: x", self.listener.reply("/status", ["db-01"]))

    def test_unknown_command_stays_silent(self):
        self.assertIsNone(self.listener.reply("/nope", []))
        self.assertIn("web-01, db-01", self.listener.reply("/help", []))


if __name__ == "__main__":
    unittest.main()
