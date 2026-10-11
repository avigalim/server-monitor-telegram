#!/usr/bin/env python3
"""Server monitor that sends alerts to Telegram.

Standard library only, Python 3.11+. See README.md for setup.
"""

import argparse
import json
import logging
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROC = Path("/proc")
SYS = Path("/sys")
CPU_SAMPLE_SECONDS = 1.0
COMMAND_POLL_SECONDS = 25
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
    # A one-off notice (e.g. a reboot): no reminders and no recovery message.
    event: bool = False


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
    "inode": {"threshold": 85, "duration": 0, "repeat_every": 21600, "mounts": ["/"]},
    "ping": {"duration": 120, "repeat_every": 1800, "targets": [], "timeout": 3},
    "temperature": {"threshold": 80, "duration": 120, "repeat_every": 3600},
    "reboot": {"duration": 0, "repeat_every": 0, "window": 600},
    "ssl": {"threshold": 14, "duration": 0, "repeat_every": 86400, "targets": [], "timeout": 5},
}

SUMMARY_DEFAULTS = {"enabled": False, "times": ["08:00"]}
COMMANDS_DEFAULTS = {"enabled": False}
KNOWN_SECTIONS = {"global", "alerts", "summary", "commands"}
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")  # no leading "-", so never read as an option
SSL_TARGET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*(:\d{1,5})?$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


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
    for target in alerts.get("ping", {}).get("targets", []):
        if not isinstance(target, str) or not HOST_RE.match(target):
            raise ConfigError(f"Bad ping target '{target}'. Use a hostname or IP address.")
    for target in alerts.get("ssl", {}).get("targets", []):
        if not isinstance(target, str) or not SSL_TARGET_RE.match(target):
            raise ConfigError(f"Bad ssl target '{target}'. Use host or host:port.")

    unknown = set(raw) - KNOWN_SECTIONS
    if unknown:
        raise ConfigError(f"Unknown section [{', '.join(sorted(unknown))}]. Known: {', '.join(sorted(KNOWN_SECTIONS))}")

    summary = {**SUMMARY_DEFAULTS, **raw.get("summary", {})}
    times = summary["times"]
    if not isinstance(times, list) or not all(isinstance(t, str) and TIME_RE.match(t) for t in times):
        raise ConfigError('summary.times must be a list of "HH:MM" strings, e.g. ["08:00", "20:00"]')
    commands = {**COMMANDS_DEFAULTS, **raw.get("commands", {})}

    return {"global": g, "alerts": alerts, "summary": summary, "commands": commands}


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


class Telegram:
    def __init__(self, token, chat_ids, timeout=10):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_ids = chat_ids
        self.timeout = timeout

    def _call(self, method, params, timeout):
        data = urllib.parse.urlencode(params).encode()
        with urllib.request.urlopen(urllib.request.Request(self.base + method, data=data), timeout=timeout) as resp:
            return resp.read()

    def send_to(self, chat_id, text):
        """Send to one chat. Returns True on success."""
        params = {"chat_id": chat_id, "text": text[:4000], "disable_web_page_preview": "true"}
        try:
            self._call("sendMessage", params, self.timeout)
            return True
        except urllib.error.HTTPError as e:
            body = e.read(300).decode("utf-8", "replace")
            log.error("Telegram rejected message for chat %s: HTTP %s %s", chat_id, e.code, body)
        except (urllib.error.URLError, OSError) as e:
            log.error("Could not reach Telegram for chat %s: %s", chat_id, e)
        return False

    def send(self, text):
        """Send to every chat. Returns True if at least one delivery succeeded."""
        results = [self.send_to(chat_id, text) for chat_id in self.chat_ids]
        return any(results)

    def get_updates(self, offset, poll_timeout):
        """Long-poll for new messages. Raises on network or HTTP errors."""
        params = {"timeout": poll_timeout, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        body = self._call("getUpdates", params, poll_timeout + 10)
        return json.loads(body).get("result", [])


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


def check_inode(cfg):
    readings = []
    t = cfg["threshold"]
    for mount in cfg["mounts"]:
        key, label = f"inode:{mount}", f"Inodes {mount}"
        try:
            st = os.statvfs(mount)
        except OSError as e:
            readings.append(Reading(key, label, True, f"cannot read usage: {e}"))
            continue
        if st.f_files == 0:
            continue  # filesystem without a fixed inode table (e.g. btrfs): nothing to monitor
        used = st.f_files - st.f_ffree
        pct = 100.0 * used / st.f_files
        detail = f"{pct:.1f}% used, {st.f_ffree} free (threshold {t}%)"
        readings.append(Reading(key, label, pct > t, detail))
    return readings


def check_ping(cfg):
    readings = []
    timeout = cfg["timeout"]
    for host in cfg["targets"]:
        key, label = f"ping:{host}", f"Ping {host}"
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(max(1, int(timeout))), "--", host],
                capture_output=True,
                timeout=timeout + 5,
            )
            ok = result.returncode == 0
            stderr = (result.stderr or b"").decode("utf-8", "replace")
            if not ok and ("not permitted" in stderr or "cap_net_raw" in stderr):
                raise CheckError(
                    "ping is not allowed to open a raw socket for this user; "
                    "see 'Ping permissions' in README.md"
                )
        except subprocess.TimeoutExpired:
            ok = False
        except FileNotFoundError:
            raise CheckError("ping not found; install iputils-ping (Debian/Ubuntu) or iputils (RHEL)")
        readings.append(Reading(key, label, not ok, "reply received" if ok else "no reply"))
    return readings


def check_temperature(cfg):
    readings = []
    t = cfg["threshold"]
    for zone in sorted((SYS / "class" / "thermal").glob("thermal_zone*")):
        try:
            celsius = int((zone / "temp").read_text().strip()) / 1000.0
            kind = (zone / "type").read_text().strip() if (zone / "type").exists() else zone.name
        except (OSError, ValueError):
            continue
        detail = f"{celsius:.1f}°C (threshold {t}°C)"
        readings.append(Reading(f"temperature:{zone.name}", f"Temperature {kind}", celsius > t, detail))
    return readings  # empty on machines without sensors, such as most VMs


def _uptime_seconds():
    try:
        return float((PROC / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def check_reboot(cfg):
    uptime = _uptime_seconds()
    if uptime is None:
        raise CheckError("cannot read /proc/uptime")
    recent = uptime < cfg["window"]
    detail = f"server restarted {format_duration(uptime)} ago" if recent else f"up {format_duration(uptime)}"
    return [Reading("reboot", "Reboot", recent, detail, event=True)]


def _cert_expiry(host, port, timeout):
    """Return the certificate's expiry as a Unix timestamp. Raises ssl.SSLError / OSError."""
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            return ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])


def check_ssl(cfg):
    readings = []
    t = cfg["threshold"]
    for target in cfg["targets"]:
        host, _, port = target.partition(":")
        key, label = f"ssl:{target}", f"SSL {target}"
        try:
            expiry = _cert_expiry(host, int(port or 443), cfg["timeout"])
        except OSError as e:  # includes ssl.SSLError (expired, untrusted, wrong host)
            readings.append(Reading(key, label, True, f"cannot verify certificate: {e}"))
            continue
        days = (expiry - time.time()) / 86400
        date = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d")
        if days < 0:
            detail = f"EXPIRED {-days:.0f} days ago ({date})"
        else:
            detail = f"expires in {days:.0f} days ({date}), warns below {t} days"
        readings.append(Reading(key, label, days < t, detail))
    return readings


COLLECTORS = {
    "cpu": check_cpu,
    "load": check_load,
    "memory": check_memory,
    "swap": check_swap,
    "disk": check_disk,
    "service": check_service,
    "endpoint": check_endpoint,
    "inode": check_inode,
    "ping": check_ping,
    "temperature": check_temperature,
    "reboot": check_reboot,
    "ssl": check_ssl,
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
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h" if hours else f"{days}d"
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
            lasted = f"\nProblem ongoing for {format_duration(bad_for)}." if bad_for >= 60 and not r.event else ""
            if not s["alerting"]:
                icon = "🔄" if r.event else "🔴"
                if self.notify(f"{icon} {tag} {r.label}: {r.detail}{lasted}"):
                    s["alerting"], s["last_notified"] = True, now
            elif repeat_every > 0 and not r.event and now - s["last_notified"] >= repeat_every:
                if self.notify(f"🔁 {tag} {r.label} still bad: {r.detail}{lasted}"):
                    s["last_notified"] = now
        elif s["alerting"]:
            if r.event:
                s.update(bad_since=None, alerting=False, last_notified=0.0)  # one-off notice, no recovery message
                return
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
            if key.startswith("_"):
                continue  # bookkeeping such as "_summary", not an alert
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
    """Run every enabled check once. Returns (readings, names of checks that could not run)."""
    now = time.time() if now is None else now
    enabled, ok_alerts, all_readings, failed = set(), set(), [], []
    for name, cfg in settings["alerts"].items():
        if not cfg["enabled"]:
            continue
        enabled.add(name)
        try:
            readings = COLLECTORS[name](cfg)
        except CheckError as e:
            log.error("Check '%s' skipped this cycle: %s", name, e)
            failed.append(name)
            continue
        except Exception:
            log.exception("Check '%s' failed, skipping it this cycle", name)
            failed.append(name)
            continue
        ok_alerts.add(name)
        for r in readings:
            engine.update(r, cfg["duration"], cfg["repeat_every"], now)
        all_readings.extend(readings)
    engine.prune(enabled, ok_alerts)
    return all_readings, failed


# ---------------------------------------------------------------------------
# Status report, scheduled summary, and bot commands
# ---------------------------------------------------------------------------


def format_report(server_name, title, readings, failed, uptime=None, age=None):
    """Human-readable overview of all readings, problems first."""
    bad = [r for r in readings if r.bad]
    ok = [r for r in readings if not r.bad]
    counts = f"{len(bad)} problem{'s' if len(bad) != 1 else ''}, {len(ok)} OK"
    if failed:
        counts += f", {len(failed)} check{'s' if len(failed) != 1 else ''} could not run"
    meta = [f"Uptime {format_duration(uptime)}"] if uptime is not None else []
    meta.append(counts)
    lines = [f"📊 [{server_name}] {title}", ". ".join(meta) + "."]
    if age is not None:
        lines.append(f"Data is {format_duration(age)} old.")
    lines += [f"⚠️ {r.label}: {r.detail}" for r in bad]
    lines += [f"❓ {name} check could not run" for name in failed]
    lines += [f"✅ {r.label}: {r.detail}" for r in ok]
    return "\n".join(lines)


class Snapshot:
    """The readings of the latest completed cycle, shared with the command thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._data = None

    def set(self, readings, failed, taken):
        with self._lock:
            self._data = (list(readings), list(failed), taken)

    def get(self):
        with self._lock:
            return self._data


def summary_slot_due(times, last_sent, now):
    """Return the scheduled time (epoch) that should have produced a summary but has not, else None."""
    today = datetime.fromtimestamp(now).date()
    slots = []
    for day in (today, today - timedelta(days=1)):
        for t in times:
            hour, minute = map(int, t.split(":"))
            slots.append(datetime.combine(day, dtime(hour, minute)).timestamp())
    past = [s for s in slots if s <= now]
    if not past:
        return None
    latest = max(past)
    return latest if latest > last_sent else None


def send_summary_if_due(settings, state, notify, readings, failed, now):
    """Send the scheduled summary when a slot has passed. A failed send is retried next cycle."""
    cfg = settings["summary"]
    if not cfg["enabled"] or not cfg["times"]:
        return
    meta = state.get("_summary")
    if meta is None:
        state["_summary"] = {"last_sent": now}  # first run: wait for the next slot instead of sending at once
        return
    if summary_slot_due(cfg["times"], meta["last_sent"], now) is None:
        return
    text = format_report(settings["global"]["server_name"], "Summary", readings, failed, _uptime_seconds())
    if notify(text):
        meta["last_sent"] = now
        log.info("Sent scheduled summary")


class CommandListener(threading.Thread):
    """Answers /status from the configured chats by long-polling Telegram."""

    def __init__(self, telegram, server_name, snapshot, stop_event):
        super().__init__(daemon=True, name="commands")
        self.telegram = telegram
        self.server_name = server_name
        self.snapshot = snapshot
        self.stop_event = stop_event

    def status_text(self):
        data = self.snapshot.get()
        if data is None:
            return f"[{self.server_name}] No data yet, the first check cycle has not finished."
        readings, failed, taken = data
        return format_report(self.server_name, "Status", readings, failed, _uptime_seconds(), time.time() - taken)

    def handle_update(self, update):
        message = update.get("message") or {}
        chat_id = str(message.get("chat", {}).get("id", ""))
        words = (message.get("text") or "").split()
        if not words or not words[0].startswith("/"):
            return
        if chat_id not in self.telegram.chat_ids:
            log.warning("Ignoring command from unauthorised chat %s", chat_id)
            return
        command = words[0].split("@")[0].lower()
        log.info("Answering %s from chat %s", command, chat_id)
        if command == "/status":
            self.telegram.send_to(chat_id, self.status_text())
        elif command in ("/start", "/help"):
            self.telegram.send_to(chat_id, f"[{self.server_name}] Commands: /status shows the current state of all checks.")

    def run(self):
        offset = None
        try:  # skip anything sent while the monitor was down
            updates = self.telegram.get_updates(-1, 0)
            offset = updates[-1]["update_id"] + 1 if updates else None
        except (OSError, ValueError) as e:
            log.warning("Could not read pending Telegram updates: %s", e)
        while not self.stop_event.is_set():
            try:
                updates = self.telegram.get_updates(offset, COMMAND_POLL_SECONDS)
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    log.error("Telegram reports another client is polling this bot (409). Only one monitor per bot "
                              "can use [commands]. Retrying in 60s.")
                else:
                    log.error("Telegram command polling failed: HTTP %s", e.code)
                self.stop_event.wait(60)
                continue
            except (OSError, ValueError) as e:
                log.warning("Telegram command polling failed: %s", e)
                self.stop_event.wait(15)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    self.handle_update(update)
                except Exception:
                    log.exception("Failed to handle Telegram update")


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
    telegram = None
    if args.dry_run:
        def notify(text):
            print(f"--- would send ---\n{text}\n------------------")
            return True
    else:
        telegram = Telegram(token, chat_ids)
        notify = telegram.send

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

    snapshot = Snapshot()
    if settings["commands"]["enabled"]:
        if telegram is None or args.once:
            log.info("Bot commands only run in normal mode (not with --dry-run or --once).")
        else:
            CommandListener(telegram, g["server_name"], snapshot, stop).start()
            log.info("Listening for /status from the configured chats.")

    while not stop.is_set():
        started = time.monotonic()
        try:
            now = time.time()
            readings, failed = run_cycle(settings, engine, now)
            snapshot.set(readings, failed, now)
            send_summary_if_due(settings, state, notify, readings, failed, now)
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
