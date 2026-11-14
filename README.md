# Server Monitor to Telegram

A small Linux server monitor that sends alerts to one or more Telegram users. Python 3.11+, standard library only, nothing to `pip install`. Every message is labeled with the server name.

It is one script, `monitor.py`, that runs in one of three modes:

| Mode      | Runs on                  | What it does                                                                                   |
|-----------|--------------------------|------------------------------------------------------------------------------------------------|
| `local`   | each server (default)    | Checks its own server and sends its own alerts. Simplest setup, one bot per server for `/status`. |
| `agent`   | each watched server      | Runs the checks and serves the readings over HTTP. Never contacts Telegram.                    |
| `central` | one separate machine     | Polls all agents, sends every alert through one bot, answers `/status` for all servers.        |

Which one to choose:

- **One server, or a few servers that should each alert on their own:** use `local`. See [Setup A](#setup-a-one-server-local-mode).
- **Several servers, one bot, and a watcher that notices when a server is down:** use `agent` on every server and `central` on a separate machine. A server that is down cannot report that itself. See [Setup B](#setup-b-many-servers-central-and-agents).

## Contents

- [What it checks](#what-it-checks)
- [Telegram setup](#telegram-setup)
- [Requirements](#requirements)
- [Setup A: one server (local mode)](#setup-a-one-server-local-mode)
- [Setup B: many servers (central and agents)](#setup-b-many-servers-central-and-agents)
- [Summary and heartbeat](#summary-and-heartbeat)
- [Bot commands](#bot-commands)
- [Settings reference](#settings-reference)
- [Ping permissions](#ping-permissions)
- [Command line](#command-line)
- [How it behaves](#how-it-behaves)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Development and testing](#development-and-testing)

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
- `ping` needs the `ping` command. See [Ping permissions](#ping-permissions).
- To find the exact name for a `service` check, use `systemctl list-units --type=service | grep -i <part of the name>`. It must be the name `systemctl status <name>` accepts.

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

Needed for `local` and `central` mode. Agents do not use Telegram.

1. In Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Each person who should get alerts opens the bot and presses **Start** (a bot cannot message someone who has not).
3. Find each person's numeric user ID: they message [@userinfobot](https://t.me/userinfobot), or you open `https://api.telegram.org/bot<TOKEN>/getUpdates` after they message your bot and read `message.from.id`.
4. For a group, add the bot to the group and use the group's chat ID (a negative number).

## Requirements

- Linux with systemd. Python 3.11 or newer, standard library only.
- Debian 12 and Ubuntu 24.04 already ship a suitable Python. **Ubuntu 22.04 ships 3.10 and needs a newer one**, see below.
- Root access (`sudo`) on every machine you install on.

Check with `python3 --version`.

### Python 3.12 on Ubuntu 22.04

The deadsnakes repository installs Python 3.12 next to the system Python. It does not replace `python3`, which Ubuntu's own tools rely on.

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12
python3.12 --version
```

Warnings from other, unrelated repositories on the same machine (for example a missing signing key) do not affect this and can be ignored. Then use `python3.12` instead of `python3` in every command below, and point the systemd unit at it. Do this when installing the unit:

```bash
sed 's|/usr/bin/python3 |/usr/bin/python3.12 |' server-monitor.service | sudo tee /etc/systemd/system/server-monitor.service
```

(For an agent or central unit, use its own file name on both sides of the command.)

## Setup A: one server (local mode)

The server checks itself and sends its own alerts. All commands run on that server.

**1. Get the files onto the server.** Copy these four files from this project to the server's home folder with `scp`, SFTP or any file transfer tool:

- `monitor.py`
- `settings.toml`
- `.env.example` (a hidden file: turn on "show hidden files" in your file tool)
- `server-monitor.service`

**2. Create the service user and folder, and move the files in:**

```bash
sudo useradd --system --home /opt/server-monitor --shell /usr/sbin/nologin servermon
sudo mkdir -p /opt/server-monitor
sudo mv monitor.py settings.toml .env.example server-monitor.service /opt/server-monitor/
cd /opt/server-monitor
```

**3. Configure:**

```bash
sudo cp .env.example .env
sudo nano .env              # TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS
sudo nano settings.toml     # server_name, thresholds, services to watch
sudo chown -R servermon: /opt/server-monitor
sudo chmod 600 .env
```

**4. Test before enabling the service:**

```bash
sudo -u servermon python3 monitor.py --test            # sends a test message to Telegram
sudo -u servermon python3 monitor.py --once --dry-run  # runs every check once, sends nothing
```

**5. Install and start the service:**

```bash
sudo cp server-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor
systemctl status server-monitor --no-pager
journalctl -u server-monitor -f
```

On Ubuntu 22.04 (Python 3.10), use `python3.12` in step 4 and the `sed` command from [Python 3.12 on Ubuntu 22.04](#python-312-on-ubuntu-2204) in step 5.

**6. Optional:** enable `[summary]` for a daily heartbeat and `[commands]` for `/status` in `settings.toml`, then `sudo systemctl restart server-monitor`. With several local servers sharing one bot, enable `[commands]` on only one of them (see [Bot commands](#bot-commands)).

## Setup B: many servers (central and agents)

```
  server A  agent  ◄──┐
  server B  agent  ◄──┼── central monitor ──► Telegram (one bot)
  server C  agent  ◄──┘     on its own machine
```

Each server runs an **agent**: the same `monitor.py`, with the same checks, but it sends nothing to Telegram. It serves its latest readings as JSON over HTTP, and only to requests that carry a secret token. The **central monitor** on a separate machine polls every agent each `check_interval`, runs the same alert logic (duration, reminders, recovery, reboot notice) and sends the messages. The agents never contact Telegram, so there is a single bot and a single poller, and `/status` works for all servers.

Because the central monitor is a different machine, a server that is down or frozen is noticed too. Its agent stops answering and you get `🔴 [web-01] Agent: agent unreachable: ...` after `agent_duration` seconds, then `🟢 [web-01] Agent recovered` when it is back. While a server is unreachable its other alerts are kept as they were, so there is no false recovery message.

Plan these before you start:

- **The central machine.** Any small Linux machine that can reach every agent. It needs very little: the monitor uses a few tens of MB of RAM and almost no CPU. It can share a machine with other light services.
- **A token for every agent.** A long random string, at least 16 characters. One shared token for all agents is the simplest. Separate tokens limit the damage of a leak to one server.
- **Network access.** Central must reach each agent's port (default `8765`). Agents need no outgoing access at all.

Generate a token (on any machine with Python):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Keep tokens out of chat, tickets and git. They only belong in the `.env` files.

### B1. Set up the central monitor

Do this first, on the central machine.

**1. Copy these four files** to the machine's home folder:

- `monitor.py`
- `central.toml.example`
- `.env.example`
- `server-monitor-central.service`

**2. Create the user and folder, and move the files in:**

```bash
sudo useradd --system --home /opt/server-monitor --shell /usr/sbin/nologin servermon
sudo mkdir -p /opt/server-monitor
sudo mv monitor.py central.toml.example .env.example server-monitor-central.service /opt/server-monitor/
cd /opt/server-monitor
sudo cp .env.example .env
sudo cp central.toml.example central.toml
```

**3. Edit `.env`** (`sudo nano .env`):

```
TELEGRAM_BOT_TOKEN=<the bot token from BotFather>
TELEGRAM_CHAT_IDS=<comma-separated chat IDs>
AGENT_TOKEN=<the agent token you generated>
```

For a separate token per server, add one variable per server instead, for example `AGENT_TOKEN_WEB_01=...`, and name it in that server's `[[servers]]` block with `token_env`.

**4. Edit `central.toml`** (`sudo nano central.toml`). Keep one `[[servers]]` block for now:

```toml
[[servers]]
name = "web-01"
url = "http://203.0.113.10:8765"
```

`name` is what appears in every message and after `/status`. Use letters, digits, `.`, `_` or `-` only. Start with `http://` for the first test. [Encrypting the connection](#b4-encrypt-the-connection) switches it to `https://` afterwards.

**5. Lock down the files and test Telegram:**

```bash
sudo chown -R servermon: /opt/server-monitor
sudo chmod 600 /opt/server-monitor/.env
sudo -u servermon python3 /opt/server-monitor/monitor.py --settings /opt/server-monitor/central.toml --test
```

You should get a test message in Telegram. Do not start the service yet. Set up the first agent first, or central will report that server as unreachable.

### B2. Set up an agent (repeat on every server)

**1. Check Python.** `python3 --version` must say 3.11 or newer. On Ubuntu 22.04, install Python 3.12 first, see [Python 3.12 on Ubuntu 22.04](#python-312-on-ubuntu-2204), and use `python3.12` below.

**2. Copy these four files** to the server's home folder:

- `monitor.py`
- `agent.toml.example`
- `.env.example`
- `server-monitor-agent.service`

**3. Create the user and folder, and move the files in:**

```bash
sudo useradd --system --home /opt/server-monitor --shell /usr/sbin/nologin servermon
sudo mkdir -p /opt/server-monitor
sudo mv monitor.py agent.toml.example .env.example server-monitor-agent.service /opt/server-monitor/
cd /opt/server-monitor
sudo cp .env.example .env
sudo cp agent.toml.example agent.toml
```

If the user already exists, the first command says so, and that is fine.

**4. Edit `.env`** (`sudo nano .env`). An agent needs no Telegram settings. Delete the two `TELEGRAM_...` lines, uncomment `AGENT_TOKEN` and set it to the same token that central uses for this server:

```
AGENT_TOKEN=<the agent token>
```

**5. Edit `agent.toml`** (`sudo nano agent.toml`). Choose what to watch. The alert options are the same as in `settings.toml`. For example:

```toml
[alerts.service]
names = ["nginx", "mariadb"]   # services that must stay active on THIS server
```

The agent's `duration` and `repeat_every` are the ones central uses, so each server keeps its own rules in one file. Leave `port = 8765` unless you need another.

**6. Lock down the files and test the checks:**

```bash
sudo chown -R servermon: /opt/server-monitor
sudo chmod 600 /opt/server-monitor/.env
sudo -u servermon python3 /opt/server-monitor/monitor.py --settings /opt/server-monitor/agent.toml --once
```

`--once` prints the JSON the agent would serve and starts no server. Check that `"failed"` is `[]` and that you see a reading for every check you enabled.

**7. Install and start the service:**

```bash
sudo cp /opt/server-monitor/server-monitor-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor-agent
systemctl status server-monitor-agent --no-pager
```

On Ubuntu 22.04, install the unit with the `sed` command from [Python 3.12 on Ubuntu 22.04](#python-312-on-ubuntu-2204) instead of `cp`:

```bash
sed 's|/usr/bin/python3 |/usr/bin/python3.12 |' /opt/server-monitor/server-monitor-agent.service | sudo tee /etc/systemd/system/server-monitor-agent.service
```

**8. Open the port to the central machine only.** The agent listens on all addresses, so the firewall is what limits who can connect. Replace `CENTRAL_IP` with the central machine's public IP:

```bash
# ufw (Ubuntu default)
sudo ufw allow from CENTRAL_IP to any port 8765 proto tcp comment 'server-monitor agent'

# firewalld
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="CENTRAL_IP" port port="8765" protocol="tcp" accept'
sudo firewall-cmd --reload
```

If your provider has its own cloud firewall, allow the port there too. Every request must carry the token as well, and a request without it gets `401` before anything else is checked.

### B3. Start central and test

**1. Add the server and poll it once** (on the central machine). This prints what central would do and sends nothing to Telegram:

```bash
sudo -u servermon python3 /opt/server-monitor/monitor.py --settings /opt/server-monitor/central.toml --once --dry-run
```

You should see `📊 Status: 1 server, 0 unreachable, 0 with problems.` and `✅ web-01: all N OK`.

**2. Install and start the central service:**

```bash
sudo cp /opt/server-monitor/server-monitor-central.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now server-monitor-central
systemctl status server-monitor-central --no-pager
```

**3. Test `/status`.** Send `/status` to the bot in Telegram. You get one line per server. `/status web-01` shows the full report for one server (names are not case sensitive).

**4. Test the alert that matters most, a server that stops answering.** On the agent server:

```bash
sudo systemctl stop server-monitor-agent
```

After about 3 minutes (2 minutes of `agent_duration` plus up to one poll interval) you get `🔴 [web-01] Agent: agent unreachable: ...`. Then:

```bash
sudo systemctl start server-monitor-agent
```

Within a minute you get `🟢 [web-01] Agent recovered`.

### B4. Encrypt the connection

Plain HTTP sends the token in clear text on every poll. The firewall rule limits who can connect, but it does not encrypt anything. Use plain HTTP only inside a private network or VPN. Otherwise give each agent a certificate.

On the agent server, create a self-signed certificate valid for one year. Replace `web-01` and the IP with the server's own:

```bash
cd /opt/server-monitor
sudo openssl req -x509 -newkey rsa:2048 -nodes -keyout agent-key.pem -out agent-cert.pem -days 365 \
  -subj "/CN=web-01" -addext "subjectAltName=IP:203.0.113.10"
sudo chown servermon: agent-key.pem agent-cert.pem && sudo chmod 600 agent-key.pem
```

Then:

1. In `agent.toml`, set `tls_cert = "agent-cert.pem"` and `tls_key = "agent-key.pem"` under `[agent]`, and restart: `sudo systemctl restart server-monitor-agent`.
2. Copy `agent-cert.pem` (the certificate only, never the key) to the central machine, for example to `/opt/server-monitor/web-01-cert.pem`, owned by `servermon`.
3. In `central.toml`, change that server's block to `url = "https://203.0.113.10:8765"` and add `ca_file = "web-01-cert.pem"`. Restart: `sudo systemctl restart server-monitor-central`.

The central monitor always verifies the certificate. A certificate from a public CA, for example Let's Encrypt on a host name, needs no `ca_file`. **A self-signed certificate expires after the number of days you chose**: renew it before then, or the server shows up as `agent unreachable: ... certificate verify failed`.

### B5. Add more servers

For each additional server:

1. Do [B2](#b2-set-up-an-agent-repeat-on-every-server) on it.
2. On the central machine, add a block to `central.toml`, and a token variable to `.env` if it has its own token:

```toml
[[servers]]
name = "db-01"
url = "https://203.0.113.11:8765"
token_env = "AGENT_TOKEN_DB_01"     # omit to use the shared AGENT_TOKEN
ca_file = "db-01-cert.pem"          # only for a self-signed certificate
```

3. `sudo systemctl restart server-monitor-central`.

Removing a block and restarting also drops that server's saved alerts.

### Notes on central and agents

- Checks run from the agent's own point of view. `endpoint`, `ping` and `ssl` checks tell you what that server can reach, not what the central machine can reach.
- If an agent's checks stop updating while it still answers, central reports `agent's data is Xm old` instead of trusting old numbers.
- The `[summary]` times in `central.toml` use the central machine's local time. Check it with `timedatectl` and set the time zone with `sudo timedatectl set-timezone <Zone/City>` if needed. Agent clocks do not matter, because central measures the age of the data, not its timestamp.
- Agent mode listens on IPv4 only. `--test` does not apply to agents, because they never contact Telegram.
- Central state is kept in `state.json` in the central settings folder.
- The same `monitor.py` file runs in every mode. Agents and central must run the same protocol version, which a normal [update](#updating) keeps in step.

## Summary and heartbeat

```toml
[summary]
enabled = true
times = ["08:00", "20:00"]   # local time of the machine running the monitor
```

At each listed time the monitor sends an overview of every check, problems first:

```
📊 [web-01] Summary
Uptime 12d 3h. 1 problem, 9 OK.
⚠️ Disk /: 91.3% used, 4.2 GiB free (threshold 85%)
✅ CPU: usage 3.1% (threshold 90%)
...
```

In central mode the summary is one overview of all servers: one line per server, with details only for problems.

It doubles as a heartbeat: alerts only arrive when something goes wrong, so silence is ambiguous. If the daily summary stops arriving, the monitor or the whole machine is down. The first run after enabling waits for the next listed time instead of sending at once, and a summary that could not be delivered is retried on the next cycle.

## Bot commands

```toml
[commands]
enabled = true
```

Send `/status` to the bot and it replies with the same overview, from the latest check cycle. Only chats listed in `TELEGRAM_CHAT_IDS` are answered; anyone else is ignored (and logged). In central mode, `/status` covers all servers and `/status <name>` shows one.

**One poller per bot.** Telegram allows only one program to poll a bot for messages. In `local` mode, if several servers share one bot token, enable `[commands]` on just one of them, or give each server its own bot. A second poller makes both fail with `409 Conflict`, which is logged. Alerts and summaries are unaffected, because sending has no such limit. Central mode avoids this limit: only the central monitor talks to Telegram. **If you reuse a bot for central, make sure no `local` monitor still has `[commands] enabled = true` on the same bot.**

## Settings reference

The three modes use the same settings file format. Alerts, `[summary]` and `[commands]` are described above and in [settings.toml](settings.toml). These are the options added for agent and central mode, see [agent.toml.example](agent.toml.example) and [central.toml.example](central.toml.example):

| Section / key                | Default      | Meaning                                                                    |
|------------------------------|--------------|----------------------------------------------------------------------------|
| `[global] mode`              | `"local"`    | `local`, `agent` or `central`                                              |
| `[global] check_interval`    | `60`         | Seconds between check cycles (agent, local) or polls of all agents (central) |
| `[global] server_name`       | hostname     | Name in messages (local). In agent mode only in the log. In central mode in the test message. |
| `[agent] listen`             | `"0.0.0.0"`  | IPv4 address to listen on                                                  |
| `[agent] port`               | `8765`       | TCP port                                                                   |
| `[agent] tls_cert`, `tls_key`| none         | Files for HTTPS. Set both or neither. Relative paths are relative to the settings file. |
| `[central] timeout`          | `10`         | Seconds to wait for each agent                                             |
| `[central] agent_duration`   | `120`        | Seconds an agent must stay unreachable before the alert                    |
| `[central] agent_repeat_every` | `1800`     | Seconds between reminders while it stays unreachable (`0` = never)         |
| `[[servers]] name`           | required     | Name in messages and after `/status`                                       |
| `[[servers]] url`            | required     | `http://host:port` or `https://host:port` of the agent                     |
| `[[servers]] token_env`      | `AGENT_TOKEN`| Name of the `.env` variable holding this agent's token                     |
| `[[servers]] ca_file`        | none         | The agent's certificate file, for a self-signed certificate                |

Environment variables: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` (local and central), `AGENT_TOKEN` or the names chosen with `token_env` (agent and central). Real environment variables take precedence over `.env`.

## Ping permissions

`ping` needs a raw socket. The shipped units grant the service exactly that one capability (`AmbientCapabilities=CAP_NET_RAW`). Without it, the unit's `NoNewPrivileges=true` stops `ping` from using its own file capability, and on kernels where `net.ipv4.ping_group_range` is `1 0` (the Debian and WSL default) the ping check cannot run. The monitor detects this and logs `Check 'ping' skipped ... not allowed to open a raw socket` instead of reporting hosts as unreachable. If you run the monitor without the shipped unit, either run it as a user that may ping, or set `net.ipv4.ping_group_range = 0 2147483647`. If `ping` is not installed: `sudo apt install iputils-ping`. The central unit does not grant it, because central runs no ping checks of its own.

## Command line

```
python3 monitor.py [--settings FILE] [--env FILE] [--once] [--dry-run] [--test] [-v]
```

- `--settings FILE` selects the settings file, and with it the mode. The shipped agent and central units pass `agent.toml` and `central.toml`.
- `--test` sends a test message to every recipient and exits (not available in agent mode).
- `--once` runs a single cycle and prints the result. In local mode it prints every reading, in agent mode the JSON it would serve, in central mode the overview of all servers.
- `--dry-run` prints the messages instead of sending them and does not save state. Central still polls the real agents.
- `-v` logs every reading on every cycle.

Note: with a `duration` above 0, a single `--once` run never alerts, because the problem has not lasted long enough yet. To see alerts fire, use a settings copy with `duration = 0`.

## How it behaves

- **State survives restarts.** Active alerts are saved in `state.json` next to the settings file, so a restart does not re-send alerts or lose the "problem started at" time.
- **Failed deliveries are retried.** If Telegram cannot be reached, the alert is not marked as sent, and the next cycle tries again. A message counts as delivered if at least one recipient received it.
- **A broken check does not stop the others.** If one check crashes or cannot run (for example `service` on a host without systemd), it is logged and skipped for that cycle while the rest continue.
- **An unreachable agent is one alert, not many.** Its other alerts stay as they were until it answers again.
- **Config changes** take effect on restart, for example `sudo systemctl restart server-monitor` (or `server-monitor-agent`, `server-monitor-central`).
- Tokens are read from `.env` (or real environment variables) and are never logged.

## Updating

Copy the new `monitor.py` over `/opt/server-monitor/monitor.py` on every machine, then restart the service there. Update the agents and the central machine in the same sitting, so they speak the same protocol version. A new example settings file never overwrites your own `settings.toml`, `agent.toml` or `central.toml`, so compare them by hand if a release adds options.

## Troubleshooting

Run `journalctl -u server-monitor-central -f` (or `-agent`, or `server-monitor` for local mode) to see the log.

| Message                                              | Cause and fix                                                                                   |
|------------------------------------------------------|-------------------------------------------------------------------------------------------------|
| `agent unreachable: ... Connection refused`          | The agent service is not running, or the port is wrong. `systemctl status server-monitor-agent`.|
| `agent unreachable: ... timed out`                   | A firewall drops the traffic. Check the agent's firewall rule and any cloud firewall, and that the rule uses central's public IP. |
| `agent refused the token`                            | The token in central's `.env` differs from the agent's `.env`. Fix one and restart that service. |
| `agent unreachable: ... certificate verify failed`   | Wrong or expired certificate, wrong `ca_file`, or the IP is not in the certificate's `subjectAltName`. See [B4](#b4-encrypt-the-connection). |
| `agent answered HTTP 503`                            | The agent just started and has not finished its first cycle. It clears within one interval.      |
| `invalid answer from agent`                          | Agent and central run different versions of `monitor.py`, or something else answers on that port. |
| `agent's data is Xm old`                             | The agent answers but its checks stopped updating. Look at its log.                              |
| `Set AGENT_TOKEN ... in .env`                        | A token is missing or shorter than 16 characters in the `.env` named in the message.             |
| `Telegram reports another client is polling this bot (409)` | Another program polls the same bot. Disable `[commands]` there, see [Bot commands](#bot-commands). |
| `Check 'service' skipped`                            | `systemctl` is missing on that host.                                                             |
| `Cannot start the agent on ...: Address already in use` | Another program uses the port. Change `[agent] port` and the firewall rule and `url` to match.|

## Development and testing

Unit tests run anywhere, including Windows, using fake `/proc` files, local test servers and no internet:

```bash
python -m unittest discover -s tests
```

To exercise the real Linux checks from Windows or macOS, use Docker (the `service` check will report that `systemctl` is missing, which is expected in a container):

```bash
docker run --rm -v "$PWD:/app:ro" python:3.13-slim python /app/monitor.py --dry-run --once
```

Testing the systemd units and the `service` check needs a real systemd host, such as WSL2 Ubuntu with systemd enabled, or a VM.

## Ideas for later

Per-alert recipients (for example, disk alerts to one person only), alert acknowledgement or muting from Telegram, and central-side checks of each server (for example a TCP or HTTP probe from the central machine, to see a server from outside).

## License

MIT, see [LICENSE](LICENSE).
