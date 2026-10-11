# Server Monitor to Telegram: Plan

Status: phase 1 built and tested.
- 36 unit tests pass; run against a real Linux /proc in Docker.
- Tested on WSL2 Ubuntu 24.04 with systemd (Python 3.12): the service check works as an unprivileged user, the unit runs as `servermon`, restarts after kill -9, stops cleanly on SIGTERM, and state.json is writable. Telegram rejected the dummy token with HTTP 401 and alerts were retried each cycle, as designed.
- Not yet done: sending a real Telegram message (needs a real bot token), and an end to end alert, repeat and recovery run with delivery succeeding.

WSL test setup: distro `Ubuntu-24.04` (systemd enabled), project installed in /opt/server-monitor with a dummy token, service stopped and disabled.

## Goal

A simple Linux server monitoring tool that sends alerts to a Telegram bot. Configured with an `.env` file (bot token, recipient user IDs) and a settings file (which alerts are enabled, thresholds, how often to repeat).

## Decisions so far

- **Language:** Python 3.11+, standard library only. No `pip install` needed.
  - `urllib` for the Telegram API, `shutil` for disk, `/proc` for CPU, memory and load.
  - Bash was rejected because per-alert state (cooldowns, sustained-duration, recovery messages) gets brittle.
- **Secrets:** `.env` file, `chmod 600`.
  - `TELEGRAM_BOT_TOKEN=...`
  - `TELEGRAM_CHAT_IDS=123456,789012` (comma-separated, multiple recipients)
- **Settings:** `settings.toml` (TOML is readable by the Python stdlib via `tomllib`, and is easy to hand-edit).
- **Runtime:** one long-running loop, run as a systemd service with auto-restart. A loop makes "bad for N minutes" logic simpler than cron or a timer.
- **State:** small JSON file remembering which alerts are active and when each last notified, so restarts do not cause duplicate alerts.

## Settings sketch

```toml
[global]
check_interval = 60        # seconds between checks
server_name = "web-01"     # included in every message

[alerts.cpu]
enabled = true
threshold = 90             # percent
duration = 300             # must stay above threshold this long (seconds)
repeat_every = 3600        # re-alert interval while still bad (seconds)

[alerts.memory]
enabled = true
threshold = 90
duration = 300
repeat_every = 3600

[alerts.disk]
enabled = true
threshold = 85
mounts = ["/", "/var"]
repeat_every = 21600

[alerts.service]
enabled = true
names = ["nginx", "postgresql"]
repeat_every = 1800
```

## Checks

Core (v1):
- CPU / load average
- Memory and swap
- Disk usage per mount
- systemd services up/down
- TCP ports / HTTP endpoints reachable

Later:
- Inode usage
- Ping / network reachability
- Temperature
- Reboot detection (uptime reset)
- SSL certificate expiry

## Noise control

- `duration`: a metric must stay past its threshold for this long before alerting, so brief spikes are ignored.
- `repeat_every`: cooldown between repeat alerts while the problem persists.
- Recovery message sent when a metric returns to normal.

## Optional extras (not in v1 unless wanted)

- Daily or weekly summary message.
- Heartbeat message so you know the monitor itself is alive.
- Bot commands such as `/status` replying with current stats. Needs polling and a whitelist of allowed user IDs.

## Deliverables

- `monitor.py` (main script)
- `.env.example`
- `settings.toml`
- `server-monitor.service` (systemd unit)
- `README.md` (install and setup steps, including creating the bot via @BotFather and finding user IDs)

## Resolved questions

1. One copy per server, messages labeled by `server_name`.
2. `/status` bot commands: later.
3. Phase 1 checks: CPU, load, memory, swap, disk, services, endpoints.

## Next steps

1. Create the bot with @BotFather, fill in `.env`, and run `python monitor.py --test`.
2. (Done on WSL Ubuntu 24.04.) Repeat the install on the real target server.
3. Deploy (see README.md).
4. Phase 2: inode, ping, temperature, reboot, SSL expiry; summary and heartbeat; `/status`.
