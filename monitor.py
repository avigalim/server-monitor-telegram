#!/usr/bin/env python3
"""Server monitor that sends alerts to Telegram.

Three modes, chosen with `mode` in the [global] settings:
  local    checks this machine and sends its own alerts (the default)
  agent    checks this machine and serves the readings over HTTP, sends nothing
  central  polls many agents and sends all alerts and answers /status for them

Standard library only, Python 3.11+. See README.md for setup.
"""

import argparse
import hmac
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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from datetime import time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
AGENT_DEFAULTS = {"listen": "0.0.0.0", "port": 8765, "tls_cert": "", "tls_key": ""}
CENTRAL_DEFAULTS = {"timeout": 10, "agent_duration": 120, "agent_repeat_every": 1800}
KNOWN_SECTIONS = {"global", "alerts", "summary", "commands", "agent", "central", "servers"}
MODES = ("local", "agent", "central")
PROTOCOL_VERSION = 1
MIN_TOKEN_LENGTH = 16
AGENT_TOKEN_ENV = "AGENT_TOKEN"
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # no spaces: it is typed after /status
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
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
    for name, value in os.environ.items():  # real environment variables win over the file
        if name.startswith(("TELEGRAM_", "AGENT_TOKEN")):
            env[name] = value
    return env


def load_settings(path):
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"Settings file not found: {path}")
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid settings file {path}: {e}")

    g = {"check_interval": 60, "server_name": socket.gethostname(), "state_file": "state.json", "mode": "local"}
    g.update(raw.get("global", {}))
    if not isinstance(g["check_interval"], (int, float)) or g["check_interval"] < 5:
        raise ConfigError("global.check_interval must be a number >= 5 (seconds)")
    if g["mode"] not in MODES:
        raise ConfigError(f"global.mode must be one of: {', '.join(MODES)}")
    base = Path(path).resolve().parent
    g["state_file"] = base / g["state_file"]

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

    agent = {**AGENT_DEFAULTS, **raw.get("agent", {})}
    port = agent["port"]
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigError("agent.port must be a number between 1 and 65535")
    if bool(agent["tls_cert"]) != bool(agent["tls_key"]):
        raise ConfigError("agent.tls_cert and agent.tls_key must be set together")
    for name in ("tls_cert", "tls_key"):
        if agent[name]:
            agent[name] = str(base / agent[name])

    central = {**CENTRAL_DEFAULTS, **raw.get("central", {})}
    if not isinstance(central["timeout"], (int, float)) or central["timeout"] <= 0:
        raise ConfigError("central.timeout must be a number above 0 (seconds)")
    for name in ("agent_duration", "agent_repeat_every"):
        if not isinstance(central[name], (int, float)) or central[name] < 0:
            raise ConfigError(f"central.{name} must be a number >= 0 (seconds)")

    servers = _load_servers(raw.get("servers", []), base) if g["mode"] == "central" else []

    return {"global": g, "alerts": alerts, "summary": summary, "commands": commands,
            "agent": agent, "central": central, "servers": servers}


def _load_servers(raw_servers, base):
    """Validate the [[servers]] list that central mode polls."""
    if not isinstance(raw_servers, list) or not raw_servers:
        raise ConfigError("Central mode needs at least one [[servers]] entry (name and url)")
    servers, seen = [], set()
    for entry in raw_servers:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not SERVER_NAME_RE.match(name):
            raise ConfigError("Every [[servers]] entry needs a name of letters, digits, '.', '_' or '-'")
        if name.lower() in seen:
            raise ConfigError(f"Duplicate server name '{name}'")
        seen.add(name.lower())
        u = urllib.parse.urlparse(entry.get("url", ""))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ConfigError(f"Server '{name}': url must look like http(s)://host:port")
        token_env = entry.get("token_env", AGENT_TOKEN_ENV)
        if not isinstance(token_env, str) or not ENV_NAME_RE.match(token_env):
            raise ConfigError(f"Server '{name}': token_env must be an environment variable name")
        ca_file = entry.get("ca_file", "")
        if not isinstance(ca_file, str):
            raise ConfigError(f"Server '{name}': ca_file must be a file path")
        servers.append({"name": name, "url": entry["url"], "token_env": token_env,
                        "ca_file": str(base / ca_file) if ca_file else ""})
    return servers


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


def collect_readings(settings):
    """Run every enabled check once.

    Returns (enabled alert names, [(alert name, readings)] for checks that ran,
    names of checks that could not run).
    """
    enabled, results, failed = set(), [], []
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
        results.append((name, readings))
    return enabled, results, failed


def run_cycle(settings, engine, now=None):
    """Run every enabled check once. Returns (readings, names of checks that could not run)."""
    now = time.time() if now is None else now
    enabled, results, failed = collect_readings(settings)
    all_readings = []
    for name, readings in results:
        cfg = settings["alerts"][name]
        for r in readings:
            engine.update(r, cfg["duration"], cfg["repeat_every"], now)
        all_readings.extend(readings)
    engine.prune(enabled, {name for name, _ in results})
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


def send_summary(settings, state, notify, make_text, now):
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
    if notify(make_text()):
        meta["last_sent"] = now
        log.info("Sent scheduled summary")


def send_summary_if_due(settings, state, notify, readings, failed, now):
    send_summary(
        settings, state, notify,
        lambda: format_report(settings["global"]["server_name"], "Summary", readings, failed, _uptime_seconds()),
        now,
    )


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
        text = self.reply(command, words[1:])
        if text is not None:
            self.telegram.send_to(chat_id, text)

    def reply(self, command, args):
        """The answer to a command, or None to stay silent."""
        if command == "/status":
            return self.status_text()
        if command in ("/start", "/help"):
            return f"[{self.server_name}] Commands: /status shows the current state of all checks."
        return None

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
# Agent mode: serve this machine's readings over HTTP
# ---------------------------------------------------------------------------


def build_agent_payload(settings, snapshot, now=None):
    """The JSON document the agent serves, or None before the first cycle has finished."""
    data = snapshot.get()
    if data is None:
        return None
    readings, failed, taken = data
    now = time.time() if now is None else now
    return {
        "version": PROTOCOL_VERSION,
        "server_name": settings["global"]["server_name"],
        "age": max(0.0, now - taken),  # seconds, so clock differences between machines do not matter
        "interval": settings["global"]["check_interval"],
        "uptime": _uptime_seconds(),
        "failed": failed,
        # The agent's own settings decide when a problem counts; central applies them.
        "policy": {
            name: {"duration": cfg["duration"], "repeat_every": cfg["repeat_every"]}
            for name, cfg in settings["alerts"].items()
            if cfg["enabled"]
        },
        "readings": [asdict(r) for r in readings],
    }


def make_agent_handler(token, get_payload):
    expected = token.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = "server-monitor-agent"
        sys_version = ""
        timeout = 10  # a stalled client cannot hold a thread forever

        def _send(self, code, body, headers=()):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if body.startswith("{") else "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            supplied = self.headers.get("Authorization", "")
            if not (supplied.startswith("Bearer ") and hmac.compare_digest(supplied[7:].encode("utf-8"), expected)):
                log.warning("Rejected request from %s: missing or wrong token", self.client_address[0])
                return self._send(401, "unauthorized\n", [("WWW-Authenticate", "Bearer")])
            if self.path != "/status":
                return self._send(404, "not found\n")
            payload = get_payload()
            if payload is None:
                return self._send(503, "no data yet, the first check cycle has not finished\n")
            self._send(200, json.dumps(payload))

        def log_message(self, fmt, *args):
            log.debug("agent http: " + fmt, *args)

    return Handler


class AgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        log.debug("Connection from %s failed: %s", client_address[0], sys.exc_info()[1])


def make_agent_server(settings, token, get_payload):
    a = settings["agent"]
    server = AgentServer((a["listen"], a["port"]), make_agent_handler(token, get_payload))
    if a["tls_cert"]:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(a["tls_cert"], a["tls_key"])
        # Handshake in the request thread, so one slow client cannot block the accept loop.
        server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    return server


def run_agent(args, settings, token, stop):
    g = settings["global"]
    snapshot = Snapshot()

    def collect_into_snapshot():
        _, results, failed = collect_readings(settings)
        snapshot.set([r for _, readings in results for r in readings], failed, time.time())

    if args.once:
        collect_into_snapshot()
        print(json.dumps(build_agent_payload(settings, snapshot), indent=1))
        return 0

    try:
        server = make_agent_server(settings, token, lambda: build_agent_payload(settings, snapshot))
    except OSError as e:
        print(f"Cannot start the agent on {settings['agent']['listen']}:{settings['agent']['port']}: {e}", file=sys.stderr)
        return 2
    threading.Thread(target=server.serve_forever, daemon=True, name="agent-http").start()
    scheme = "https" if settings["agent"]["tls_cert"] else "http"
    log.info("Agent %s serving %s on %s port %s, checking every %ss: %s", g["server_name"], scheme,
             settings["agent"]["listen"], settings["agent"]["port"], g["check_interval"],
             ", ".join(n for n, c in settings["alerts"].items() if c["enabled"]) or "nothing enabled")

    while not stop.is_set():
        started = time.monotonic()
        try:
            collect_into_snapshot()
        except Exception:
            log.exception("Check cycle failed")
        stop.wait(max(0.0, g["check_interval"] - (time.monotonic() - started)))

    server.shutdown()
    server.server_close()
    log.info("Stopped.")
    return 0


# ---------------------------------------------------------------------------
# Central mode: poll agents, alert for all of them, answer /status for all
# ---------------------------------------------------------------------------


class AgentError(Exception):
    """An agent could not be read. The message is shown in the alert."""


def parse_agent_payload(body):
    try:
        data = json.loads(body)
        if data.get("version") != PROTOCOL_VERSION:
            raise ValueError(f"protocol version {data.get('version')}, expected {PROTOCOL_VERSION}")
        return {
            "readings": [
                Reading(str(r["key"]), str(r["label"]), bool(r["bad"]), str(r["detail"]), bool(r.get("event", False)))
                for r in data["readings"]
            ],
            "failed": [str(f) for f in data["failed"]],
            "age": float(data["age"]),
            "interval": float(data["interval"]),
            "uptime": None if data.get("uptime") is None else float(data["uptime"]),
            "policy": {
                str(name): {"duration": float(p["duration"]), "repeat_every": float(p["repeat_every"])}
                for name, p in data["policy"].items()
            },
        }
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise AgentError(f"invalid answer from agent ({e})")


def fetch_agent(server, token, timeout):
    """Read one agent. Raises AgentError with a short reason on any failure."""
    request = urllib.request.Request(
        server["url"].rstrip("/") + "/status",
        headers={"Authorization": f"Bearer {token}", "User-Agent": "server-monitor-central/1.0"},
    )
    context = None
    if server["url"].lower().startswith("https"):
        try:
            context = ssl.create_default_context(cafile=server["ca_file"] or None)
        except OSError as e:
            raise AgentError(f"cannot load ca_file: {e}")
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
            body = resp.read(2_000_000)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AgentError("agent refused the token")
        raise AgentError(f"agent answered HTTP {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise AgentError(f"agent unreachable: {getattr(e, 'reason', e)}")
    return parse_agent_payload(body)


class Fleet:
    """The latest result for each polled server, shared with the command thread.

    An entry keeps the last good readings even while the agent is unreachable,
    so /status can still show what was known before it went silent.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def record(self, name, data, error, taken):
        with self._lock:
            old = self._entries.get(name) or {}
            if data is not None:
                old = {"readings": data["readings"], "failed": data["failed"], "uptime": data["uptime"], "taken": taken}
            self._entries[name] = {"readings": None, "failed": [], "uptime": None, "taken": None, **old, "error": error}

    def get(self, name):
        with self._lock:
            entry = self._entries.get(name)
            return dict(entry) if entry else None

    def rows(self, names):
        return [(name, self.get(name)) for name in names]


def central_cycle(settings, engines, fleet, tokens, now, fetch=fetch_agent):
    """Poll every agent once and feed the readings to that server's alert engine."""
    servers = settings["servers"]
    c = settings["central"]
    with ThreadPoolExecutor(max_workers=min(16, len(servers))) as pool:
        futures = {s["name"]: pool.submit(fetch, s, tokens[s["name"]], c["timeout"]) for s in servers}
    for s in servers:
        name, engine = s["name"], engines[s["name"]]
        data, error = None, None
        try:
            data = futures[name].result()
        except AgentError as e:
            error = str(e)
        except Exception as e:
            log.exception("Polling %s failed", name)
            error = f"internal error while reading the agent: {e}"
        if data is not None and data["age"] > max(120.0, 3 * data["interval"]):
            error = f"agent's data is {format_duration(data['age'])} old, its checks may be stuck"
            data = None
        if error:
            log.warning("[%s] %s", name, error)
        # An unreachable server is itself the problem: its other alerts stay as they were.
        engine.update(Reading("agent", "Agent", error is not None, error or "reachable"),
                      c["agent_duration"], c["agent_repeat_every"], now)
        if data is not None:
            policy = data["policy"]
            for r in data["readings"]:
                p = policy.get(r.key.split(":", 1)[0], {"duration": 0.0, "repeat_every": 3600.0})
                engine.update(r, p["duration"], p["repeat_every"], now)
            engine.prune(set(policy) | {"agent"}, (set(policy) - set(data["failed"])) | {"agent"})
        fleet.record(name, data, error, now - data["age"] if data else None)


def format_fleet_report(title, rows, now):
    """Overview of all servers: one line each, with details only where something is wrong."""
    body, down, troubled = [], 0, 0
    for name, entry in rows:
        if entry is None:
            body.append(f"⏳ {name}: no data yet")
            continue
        readings, failed, error = entry["readings"], entry["failed"], entry["error"]
        bad = [r for r in readings or [] if r.bad]
        total = len(readings or [])
        counts = f"{len(bad)} problem{'s' if len(bad) != 1 else ''}, {total - len(bad)} OK"
        if failed:
            counts += f", {len(failed)} check{'s' if len(failed) != 1 else ''} could not run"
        if error:
            down += 1
            body.append(f"🔴 {name}: {error}")
            if readings is not None:
                body.append(f"   Last data {format_duration(now - entry['taken'])} ago: {counts}.")
        elif bad or failed:
            troubled += 1
            body.append(f"⚠️ {name}: {counts}")
        else:
            body.append(f"✅ {name}: all {total} OK")
        if not error or readings is not None:
            body += [f"   {r.label}: {r.detail}" for r in bad]
            body += [f"   {check} check could not run" for check in failed]
    head = f"📊 {title}: {len(rows)} server{'s' if len(rows) != 1 else ''}, {down} unreachable, {troubled} with problems."
    return "\n".join([head] + body)


class CentralCommandListener(CommandListener):
    """/status for the whole fleet, or /status <name> for one server."""

    def __init__(self, telegram, server_name, names, fleet, stop_event):
        super().__init__(telegram, server_name, Snapshot(), stop_event)
        self.names = names
        self.fleet = fleet

    def one_server(self, name):
        entry = self.fleet.get(name)
        if entry is None:
            return f"[{name}] No data yet, the first poll has not finished."
        if entry["readings"] is None:
            return f"🔴 [{name}] {entry['error']}\nNo data has been received from this server yet."
        age = time.time() - entry["taken"]
        text = format_report(name, "Status", entry["readings"], entry["failed"], entry["uptime"], age)
        return f"🔴 [{name}] {entry['error']}\n{text}" if entry["error"] else text

    def reply(self, command, args):
        if command == "/status":
            if not args:
                return format_fleet_report("Status", self.fleet.rows(self.names), time.time())
            wanted = {n.lower(): n for n in self.names}.get(args[0].lower())
            if wanted is None:
                return f"Unknown server '{args[0][:40]}'. Known servers: {', '.join(self.names)}"
            return self.one_server(wanted)
        if command in ("/start", "/help"):
            return "Commands: /status shows all servers, /status <name> shows one.\nServers: " + ", ".join(self.names)
        return None


def server_tokens(settings, env):
    tokens = {s["name"]: env.get(s["token_env"], "") for s in settings["servers"]}
    bad = sorted({s["token_env"] for s in settings["servers"] if len(tokens[s["name"]]) < MIN_TOKEN_LENGTH})
    if bad:
        raise ConfigError(f"Set {', '.join(bad)} in .env to the agent's token (at least {MIN_TOKEN_LENGTH} characters)")
    return tokens


def run_central(args, settings, tokens, telegram, notify, stop):
    g = settings["global"]
    names = [s["name"] for s in settings["servers"]]
    state = {} if args.dry_run else load_state(g["state_file"])
    saved = state.get("servers") if isinstance(state.get("servers"), dict) else {}
    # One alert state per server; servers removed from the settings are forgotten.
    state["servers"] = {n: saved[n] if isinstance(saved.get(n), dict) else {} for n in names}
    engines = {n: AlertEngine(n, notify, state["servers"][n]) for n in names}
    fleet = Fleet()

    log.info("Central monitor %s polling %d server%s every %ss: %s", g["server_name"], len(names),
             "" if len(names) == 1 else "s", g["check_interval"], ", ".join(names))

    if settings["commands"]["enabled"]:
        if telegram is None or args.once:
            log.info("Bot commands only run in normal mode (not with --dry-run or --once).")
        else:
            CentralCommandListener(telegram, g["server_name"], names, fleet, stop).start()
            log.info("Listening for /status from the configured chats.")

    while not stop.is_set():
        started = time.monotonic()
        try:
            now = time.time()
            central_cycle(settings, engines, fleet, tokens, now)
            send_summary(settings, state, notify, lambda: format_fleet_report("Summary", fleet.rows(names), now), now)
            if not args.dry_run:
                save_state(g["state_file"], state)
        except Exception:
            log.exception("Poll cycle failed")
        if args.once:
            print(format_fleet_report("Status", fleet.rows(names), time.time()))
            break
        stop.wait(max(0.0, g["check_interval"] - (time.monotonic() - started)))

    log.info("Stopped.")
    return 0


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

    for stream in (sys.stdout, sys.stderr):  # messages contain emoji; a legacy console codec must not crash on them
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.once) else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        settings = load_settings(Path(args.settings))
        env = load_env(Path(args.env))
        mode = settings["global"]["mode"]
        token = env.get("TELEGRAM_BOT_TOKEN", "")
        chat_ids = [c.strip() for c in env.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
        agent_token = env.get(AGENT_TOKEN_ENV, "")
        if mode == "agent":
            if args.test:
                raise ConfigError("--test does nothing in agent mode: an agent never contacts Telegram")
            if not args.once and len(agent_token) < MIN_TOKEN_LENGTH:
                raise ConfigError(f"{AGENT_TOKEN_ENV} must be set in .env (at least {MIN_TOKEN_LENGTH} characters)")
        elif not args.dry_run and (not token or not chat_ids):
            raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS must be set in .env")
        tokens = server_tokens(settings, env) if mode == "central" else {}
    except ConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        return 2

    g = settings["global"]
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    if mode == "agent":
        return run_agent(args, settings, agent_token, stop)

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

    if mode == "central":
        return run_central(args, settings, tokens, telegram, notify, stop)

    state = {} if args.dry_run else load_state(g["state_file"])
    engine = AlertEngine(g["server_name"], notify, state)

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
