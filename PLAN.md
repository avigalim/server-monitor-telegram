# Server Monitor to Telegram: Plan

Status: phase 1 and phase 2 built and tested.
- 71 unit tests pass.
- Verified on WSL2 Ubuntu 24.04 with systemd: unit runs as `servermon`, restarts after kill -9, stops cleanly.
- Verified with a real bot: alert, reminder and recovery messages, the reboot notice, and the scheduled summary.
- Phase 2 added: inode, ping, temperature, reboot and ssl checks; scheduled summary (also the heartbeat); `/status` command.
- Found and fixed during phase 2 testing: `ping` failed under the unit's NoNewPrivileges (the unit now grants CAP_NET_RAW, and a permission failure is reported as a skipped check, not "no reply").
- Known limitation: bot commands need one poller per bot token, so enable `[commands]` on one server only, or use one bot per server.

Phase 3 (built, tested locally and verified on real servers): agent and central modes in the same `monitor.py`.
- Agent: runs the checks, serves JSON at `/status` over HTTP or HTTPS with a bearer token, never contacts Telegram.
- Central: on a separate VPS, polls all agents, runs the existing alert logic per server, sends through one bot, answers `/status` and `/status <name>`. An unreachable or stuck agent is itself an alert.
- Decisions: agent over SSH (no extra key management); alert policy (duration, repeat_every) set in each agent's own file; central only holds the server list and tokens.
- 102 unit tests pass. Verified end to end on Windows with a real agent and central process, including HTTPS with a self-signed certificate.
- Verified on real Ubuntu servers (central on 24.04, one agent on 22.04 with Python 3.12 from deadsnakes) with a real bot: `/status`, the unreachable alert and the recovery message. Run over plain HTTP so far; HTTPS on a real agent is still to do.

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

1. Deploy to the real target server (see README.md) and trim `settings.toml` to the services, mounts and hosts that matter there.
2. Enable `[summary]` so a missing daily message tells you the server is down.
3. Ideas for later: per-alert recipients, muting alerts from Telegram, a multi-server overview.
