# Server Monitor to Telegram

A small Linux server monitor that sends alerts to one or more Telegram users. Python 3.11+, standard library only, nothing to `pip install`. Run one copy per server; every message is labeled with the server name.

## What it checks

| Alert      | Measures                                              | Key options                    |
|------------|-------------------------------------------------------|--------------------------------|
| `cpu`      | CPU usage %, sampled over 1 second each cycle         | `threshold`                    |
| `load`     | 5-minute load average per core                        | `threshold`                    |
| `memory`   | Memory in use %, excluding reclaimable cache          | `threshold`                    |
| `swap`     | Swap in use % (skipped if no swap)                    | `threshold`                    |
| `disk`     | Disk usage % for each mount point                     | `threshold`, `mounts`          |
| `service`  | systemd services that must be `active`                | `names`                        |
| `endpoint` | TCP port opens, or HTTP status below 400              | `targets`, `timeout`           |
| `inode`    | Inode usage % for each mount point                    | `threshold`, `mounts`          |
| `ping`     | Hosts answer ping (uses the system `ping` command)    | `targets`, `timeout`           |
| `temperature` | Sensor temperatures in °C from `/sys/class/thermal` | `threshold`                    |
| `reboot`   | One-off notice when the server has just restarted     | `window` (seconds)             |
| `ssl`      | TLS certificate expiry, or an invalid certificate     | `targets`, `threshold` (days)  |

Notes on the newer checks:
- `reboot` sends one 🔄 message per restart. It has no reminders and no "recovered" message, and `repeat_every` does not apply.
- `temperature` reports nothing on machines without sensors, which includes most VMs and WSL.
- `ssl` does a direct TLS handshake, so use ports like 443, 465, 993 or 995. STARTTLS ports such as 587 are not supported. Certificates that are expired, self-signed or issued for another name are reported as problems.
- `ping` needs the `ping` command. See "Ping permissions" below.

Every alert also takes `enabled`, `duration` (seconds the problem must last before the first alert, to ignore short spikes) and `repeat_every` (seconds between reminders, `0` = never remind). A recovery message is sent when a problem clears. See [settings.toml](settings.toml) for all options with comments.

Messages look like:

```
🔴 [web-01] Disk /: 91.3% used, 4.2 GiB free (threshold 85%)
🔁 [web-01] Disk / still bad: 91.5% used, 4.1 GiB free (threshold 85%)
   Problem ongoing for 6h.
🟢 [web-01] Disk / recovered: 72.0% used, 20.1 GiB free (threshold 85%)
   Problem lasted 6h 12m.
```

## Summary and heartbeat

```toml
[summary]
enabled = true
times = ["08:00", "20:00"]   # server local time
```

At each listed time the monitor sends an overview of every check, problems first:

```
📊 [web-01] Summary
Uptime 12d 3h. 1 problem, 9 OK.
⚠️ Disk /: 91.3% used, 4.2 GiB free (threshold 85%)
✅ CPU: usage 3.1% (threshold 90%)
...
```

It doubles as a heartbeat: alerts only arrive when something goes wrong, so silence is ambiguous. If the daily summary stops arriving, the monitor or the whole server is down. The first run after enabling waits for the next listed time instead of sending at once, and a summary that could not be delivered is retried on the next cycle.

## Bot commands

```toml
[commands]
enabled = true
```

Send `/status` to the bot and it replies with the same overview, from the latest check cycle. Only chats listed in `TELEGRAM_CHAT_IDS` are answered; anyone else is ignored (and logged).

**One server per bot.** Telegram allows only one program to poll a bot for messages. If several servers share one bot token, enable `[commands]` on just one of them, or give each server its own bot. A second poller makes both fail with `409 Conflict`, which is logged. Alerts and summaries are unaffected, because sending has no such limit.

## Telegram setup

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Each person who should get alerts opens the bot and presses **Start** (a bot cannot message someone who has not).
3. Find each person's numeric user ID: they message [@userinfobot](https://t.me/userinfobot), or you open `https://api.telegram.org/bot<TOKEN>/getUpdates` after they message your bot and read `message.from.id`.
4. For a group, add the bot to the group and use the group's chat ID (a negative number).

## Install on a server

```bash
sudo useradd --system --home /opt/server-monitor --shell /usr/sbin/nologin servermon
sudo mkdir -p /opt/server-monitor
sudo cp monitor.py settings.toml .env.example server-monitor.service /opt/server-monitor/
cd /opt/server-monitor

sudo cp .env.example .env
sudo nano .env              # add the bot token and chat IDs
sudo nano settings.toml     # set server_name, thresholds, services to watch

sudo chown -R servermon: /opt/server-monitor
sudo chmod 600 .env

# Check the setup before enabling the service
sudo -u servermon python3 monitor.py --test        # sends a test message
sudo -u servermon python3 monitor.py --once --dry-run

sudo cp server-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
journalctl -u server-monitor -f
```

Requires Python 3.11 or newer (`python3 --version`). Debian 12 and Ubuntu 24.04 qualify; Ubuntu 22.04 ships 3.10 and needs a newer Python installed.

## Ping permissions

`ping` needs a raw socket. The shipped unit grants the service exactly that one capability (`AmbientCapabilities=CAP_NET_RAW`). Without it, the unit's `NoNewPrivileges=true` stops `ping` from using its own file capability, and on kernels where `net.ipv4.ping_group_range` is `1 0` (the Debian and WSL default) the ping check cannot run. The monitor detects this and logs `Check 'ping' skipped ... not allowed to open a raw socket` instead of reporting hosts as unreachable. If you run the monitor without the shipped unit, either run it as a user that may ping, or set `net.ipv4.ping_group_range = 0 2147483647`. If `ping` is not installed: `sudo apt install iputils-ping`.

## Command line

```
python3 monitor.py [--settings FILE] [--env FILE] [--once] [--dry-run] [--test] [-v]
```

- `--test` sends a test message to every recipient and exits.
- `--once` runs a single check cycle and prints every reading.
- `--dry-run` prints the messages instead of sending them and does not save state.
- `-v` logs every reading on every cycle.

Note: with a `duration` above 0, a single `--once` run never alerts, because the problem has not lasted long enough yet. To see alerts fire, use a settings copy with `duration = 0`.

## How it behaves

- **State survives restarts.** Active alerts are saved in `state.json` next to the settings file, so a restart does not re-send alerts or lose the "problem started at" time.
- **Failed deliveries are retried.** If Telegram cannot be reached, the alert is not marked as sent, and the next cycle tries again. A message counts as delivered if at least one recipient received it.
- **A broken check does not stop the others.** If one check crashes or cannot run (for example `service` on a host without systemd), it is logged and skipped for that cycle while the rest continue.
- **Config changes** take effect on restart: `sudo systemctl restart server-monitor`.
- The bot token is read from `.env` (or real environment variables, which take precedence) and is never logged.

## Development and testing

Unit tests run anywhere, including Windows, using fake `/proc` files and no network:

```bash
python -m unittest discover -s tests
```

To exercise the real Linux checks from Windows or macOS, use Docker (the `service` check will report that `systemctl` is missing, which is expected in a container):

```bash
docker run --rm -v "$PWD:/app:ro" python:3.13-slim python /app/monitor.py --dry-run --once
```

Testing the systemd unit and the `service` check needs a real systemd host, such as WSL2 Ubuntu with systemd enabled, or a VM.

## Ideas for later

Per-alert recipients (for example, disk alerts to one person only), alert acknowledgement or muting from Telegram, and a multi-server overview.

## License

MIT, see [LICENSE](LICENSE).
