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

Every alert also takes `enabled`, `duration` (seconds the problem must last before the first alert, to ignore short spikes) and `repeat_every` (seconds between reminders, `0` = never remind). A recovery message is sent when a problem clears. See [settings.toml](settings.toml) for all options with comments.

Messages look like:

```
🔴 [web-01] Disk /: 91.3% used, 4.2 GiB free (threshold 85%)
🔁 [web-01] Disk / still bad: 91.5% used, 4.1 GiB free (threshold 85%)
   Problem ongoing for 6h.
🟢 [web-01] Disk / recovered: 72.0% used, 20.1 GiB free (threshold 85%)
   Problem lasted 6h 12m.
```

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

## Not yet included

Planned for later: inode, ping, temperature, reboot detection and SSL expiry checks; daily summary and heartbeat messages; a `/status` bot command.
