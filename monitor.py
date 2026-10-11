#!/usr/bin/env python3
"""Server monitor that sends alerts to Telegram.

Standard library only, Python 3.11+. See README.md for setup.
"""

import argparse
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROC = Path("/proc")
CPU_SAMPLE_SECONDS = 1.0
_sleep = time.sleep

log = logging.getLogger("monitor")


class ConfigError(Exception):
    pass


class CheckError(Exception):
    """A check cannot run on this host. Logged as one line, without a traceback."""


@dataclass
class Reading:
    """One measured thing (e.g. disk usage of /) and whether it is currently bad."""

    key: str
    label: str
    bad: bool
    detail: str


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULTS = {
    "cpu": {"threshold": 90, "duration": 300, "repeat_every": 3600},
    "load": {"threshold": 1.5, "duration": 300, "repeat_every": 3600},
    "memory": {"threshold": 90, "duration": 300, "repeat_every": 3600},
    "swap": {"threshold": 80, "duration": 300, "repeat_every": 3600},
    "disk": {"threshold": 85, "duration": 0, "repeat_every": 21600, "mounts": ["/"]},
    "service": {"duration": 60, "repeat_every": 1800, "names": []},
    "endpoint": {"duration": 120, "repeat_every": 1800, "targets": [], "timeout": 5},
}


def load_env(path):
    env = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            env[name.strip()] = value
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def load_settings(path):
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Settings file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid settings file {path}: {e}")

    g = {"check_interval": 60, "server_name": socket.gethostname(), "state_file": "state.json"}
    g.update(raw.get("global", {}))
    if not isinstance(g["check_interval"], (int, float)) or g["check_interval"] < 5:
        raise ConfigError("global.check_interval must be a number >= 5 (seconds)")
    g["state_file"] = Path(path).resolve().parent / g["state_file"]

    alerts = {}
    for name, cfg in raw.get("alerts", {}).items():
        if name not in DEFAULTS:
            raise ConfigError(f"Unknown alert '{name}'. Known alerts: {', '.join(DEFAULTS)}")
        merged = {"enabled": True, **DEFAULTS[name], **cfg}
        alerts[name] = merged

    for target in alerts.get("endpoint", {}).get("targets", []):
        u = urllib.parse.urlparse(target)
        if u.scheme not in ("tcp", "http", "https") or not u.hostname:
            raise ConfigError(f"Bad endpoint target '{target}'. Use tcp://host:port or http(s)://...")
        if u.scheme == "tcp" and u.port is None:
            raise ConfigError(f"Bad endpoint target '{target}'. A tcp:// target needs a port.")

    return {"global": g, "alerts": alerts}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class Telegram:
    def __init__(self, token, chat_ids, timeout=10):
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_ids = chat_ids
        self.timeout = timeout

    def send(self, text):
        """Send to every chat. Returns True if at least one delivery succeeded."""
        delivered = False
        for chat_id in self.chat_ids:
            data = urllib.parse.urlencode(
                {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
            ).encode()
            try:
                with urllib.request.urlopen(urllib.request.Request(self.url, data=data), timeout=self.timeout) as resp:
                    resp.read()
                delivered = True
            except urllib.error.HTTPError as e:
                body = e.read(300).decode("utf-8", "replace")
                log.error("Telegram rejected message for chat %s: HTTP %s %s", chat_id, e.code, body)
            except (urllib.error.URLError, OSError) as e:
                log.error("Could not reach Telegram for chat %s: %s", chat_id, e)
        return delivered


# ---------------------------------------------------------------------------
# Checks. Each takes its alert settings and returns a list of Reading.
# ---------------------------------------------------------------------------


def _human_bytes(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _read_cpu_times():
    values = [int(v) for v in (PROC / "stat").read_text().splitlines()[0].split()[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
    return idle, sum(values[:8])


def check_cpu(cfg):
    idle1, total1 = _read_cpu_times()
    _sleep(CPU_SAMPLE_SECONDS)
    idle2, total2 = _read_cpu_times()
    elapsed = total2 - total1
    if elapsed <= 0:
        raise RuntimeError("CPU counters did not advance")
    usage = 100.0 * (1 - (idle2 - idle1) / elapsed)
    t = cfg["threshold"]
    return [Reading("cpu", "CPU", usage > t, f"usage {usage:.1f}% (threshold {t}%)")]


def check_load(cfg):
    load5 = float((PROC / "loadavg").read_text().split()[1])
    cores = os.cpu_count() or 1
    per_core = load5 / cores
    t = cfg["threshold"]
    detail = f"5-min load {load5:.2f} on {cores} cores, {per_core:.2f} per core (threshold {t})"
    return [Reading("load", "Load", per_core > t, detail)]


def _meminfo():
    info = {}
    for line in (PROC / "meminfo").read_text().splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts:
            info[name] = int(parts[0]) * 1024  # kB -> bytes
    return info


def check_memory(cfg):
    info = _meminfo()
    total = info["MemTotal"]
    available = info.get("MemAvailable", info["MemFree"] + info.get("Buffers", 0) + info.get("Cached", 0))
    used = total - available
    pct = 100.0 * used / total
    t = cfg["threshold"]
    detail = f"{pct:.1f}% used, {_human_bytes(used)} of {_human_bytes(total)} (threshold {t}%)"
    return [Reading("memory", "Memory", pct > t, detail)]


def check_swap(cfg):
    info = _meminfo()
    total = info.get("SwapTotal", 0)
    if total == 0:
        return []  # no swap configured, nothing to monitor
    used = total - info["SwapFree"]
    pct = 100.0 * used / total
    t = cfg["threshold"]
    detail = f"{pct:.1f}% used, {_human_bytes(used)} of {_human_bytes(total)} (threshold {t}%)"
    return [Reading("swap", "Swap", pct > t, detail)]


def check_disk(cfg):
    readings = []
    t = cfg["threshold"]
    for mount in cfg["mounts"]:
        key, label = f"disk:{mount}", f"Disk {mount}"
        try:
            usage = shutil.disk_usage(mount)
        except OSError as e:
            readings.append(Reading(key, label, True, f"cannot read usage: {e}"))
            continue
        pct = 100.0 * usage.used / (usage.used + usage.free)  # same basis as `df`
        detail = f"{pct:.1f}% used, {_human_bytes(usage.free)} free (threshold {t}%)"
        readings.append(Reading(key, label, pct > t, detail))
    return readings


def check_service(cfg):
    readings = []
    for name in cfg["names"]:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", name], capture_output=True, text=True, timeout=10
            )
            state = result.stdout.strip() or "unknown"
        except subprocess.TimeoutExpired:
            state = "unknown (systemctl timed out)"
        except FileNotFoundError:
            raise CheckError("systemctl not found; the service alert needs a systemd host")
        readings.append(Reading(f"service:{name}", f"Service {name}", state != "active", f"status: {state}"))
    return readings


def _probe(target, timeout):
    u = urllib.parse.urlparse(target)
    if u.scheme == "tcp":
        with socket.create_connection((u.hostname, u.port), timeout=timeout):
            return "reachable"
    request = urllib.request.Request(target, headers={"User-Agent": "server-monitor/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return f"reachable (HTTP {resp.status})"


def check_endpoint(cfg):
    readings = []
    for target in cfg["targets"]:
        key, label = f"endpoint:{target}", f"Endpoint {target}"
        try:
            readings.append(Reading(key, label, False, _probe(target, cfg["timeout"])))
        except urllib.error.HTTPError as e:
            readings.append(Reading(key, label, True, f"HTTP {e.code}"))
        except (OSError, ValueError) as e:
            readings.append(Reading(key, label, True, f"unreachable: {e}"))
    return readings


COLLECTORS = {
    "cpu": check_cpu,
    "load": check_load,
    "memory": check_memory,
    "swap": check_swap,
    "disk": check_disk,
    "service": check_service,
    "endpoint": check_endpoint,
}


# ---------------------------------------------------------------------------
# Alert state machine
# ---------------------------------------------------------------------------


def format_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m" if minutes else f"{hours}h"


class AlertEngine:
    """Decides when to alert, remind and recover, and remembers it in `state`.

    `notify(text)` must return True if the message was delivered. A failed
    delivery leaves the state untouched so the next cycle retries.
    """

    def __init__(self, server_name, notify, state):
        self.server_name = server_name
        self.notify = notify
        self.state = state
        self.seen = set()

    def update(self, r, duration, repeat_every, now):
        self.seen.add(r.key)
        s = self.state.setdefault(r.key, {"bad_since": None, "alerting": False, "last_notified": 0.0})
        tag = f"[{self.server_name}]"
        log.debug("%s bad=%s %s", r.label, r.bad, r.detail)

        if r.bad:
            if s["bad_since"] is None:
                s["bad_since"] = now
            bad_for = now - s["bad_since"]
            if bad_for < duration:
                return
            lasted = f"\nProblem ongoing for {format_duration(bad_for)}." if bad_for >= 60 else ""
            if not s["alerting"]:
                if self.notify(f"🔴 {tag} {r.label}: {r.detail}{lasted}"):
                    s["alerting"], s["last_notified"] = True, now
            elif repeat_every > 0 and now - s["last_notified"] >= repeat_every:
                if self.notify(f"🔁 {tag} {r.label} still bad: {r.detail}{lasted}"):
                    s["last_notified"] = now
        elif s["alerting"]:
            lasted = format_duration(now - s["bad_since"])
            if self.notify(f"🟢 {tag} {r.label} recovered: {r.detail}\nProblem lasted {lasted}."):
                s.update(bad_since=None, alerting=False, last_notified=0.0)
        else:
            s["bad_since"] = None

    def prune(self, enabled, ok_alerts):
        """Forget state for things no longer monitored.

        A key is dropped if its alert was disabled, or if its alert ran fine
        this cycle and no longer reports it. Alerts whose check crashed keep
        their state, so a transient error does not erase an active alert.
        """
        for key in list(self.state):
            alert = key.split(":", 1)[0]
            if alert not in enabled or (alert in ok_alerts and key not in self.seen):
                del self.state[key]
        self.seen = set()


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("Ignoring unreadable state file %s: %s", path, e)
        return {}


def save_state(path, state):
    tmp = Path(f"{path}.tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def run_cycle(settings, engine, now=None):
    now = time.time() if now is None else now
    enabled, ok_alerts = set(), set()
    for name, cfg in settings["alerts"].items():
        if not cfg["enabled"]:
            continue
        enabled.add(name)
        try:
            readings = COLLECTORS[name](cfg)
        except CheckError as e:
            log.error("Check '%s' skipped this cycle: %s", name, e)
            continue
        except Exception:
            log.exception("Check '%s' failed, skipping it this cycle", name)
            continue
        ok_alerts.add(name)
        for r in readings:
            engine.update(r, cfg["duration"], cfg["repeat_every"], now)
    engine.prune(enabled, ok_alerts)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(description="Server monitor with Telegram alerts")
    p.add_argument("--env", default=str(BASE_DIR / ".env"), help="path to .env file")
    p.add_argument("--settings", default=str(BASE_DIR / "settings.toml"), help="path to settings file")
    p.add_argument("--once", action="store_true", help="run a single check cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="print messages instead of sending; do not save state")
    p.add_argument("--test", action="store_true", help="send a test message to all recipients and exit")
    p.add_argument("-v", "--verbose", action="store_true", help="log every reading")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.once) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        settings = load_settings(Path(args.settings))
        env = load_env(Path(args.env))
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        chat_ids = [c.strip() for c in env.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
        if not args.dry_run and (not token or not chat_ids):
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS must be set in .env")
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    g = settings["global"]
    if args.dry_run:
        def notify(text):
            print(f"--- would send ---\n{text}\n------------------")
            return True
    else:
        notify = Telegram(token, chat_ids).send

    if args.test:
        ok = notify(f"✅ [{g['server_name']}] Test message from server monitor.")
        print("Test message sent." if ok else "Test message FAILED, see log above.")
        return 0 if ok else 1

    state = {} if args.dry_run else load_state(g["state_file"])
    engine = AlertEngine(g["server_name"], notify, state)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    log.info("Monitoring %s every %ss: %s", g["server_name"], g["check_interval"],
             ", ".join(n for n, c in settings["alerts"].items() if c["enabled"]) or "nothing enabled")

    while not stop.is_set():
        started = time.monotonic()
        try:
            run_cycle(settings, engine)
            if not args.dry_run:
                save_state(g["state_file"], state)
        except Exception:
            log.exception("Check cycle failed")
        if args.once:
            break
        stop.wait(max(0.0, g["check_interval"] - (time.monotonic() - started)))

    log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
