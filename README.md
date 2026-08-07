# gexbot

An automated gamma-exposure (GEX) options trader for SPY and QQQ. Runs as an
always-on daemon, executes through Robinhood's agentic MCP endpoint, and serves
a live dashboard so you can watch it think.

**All times are CENTRAL.** It ships in paper mode and stays there until you
deliberately arm it.

---

## The thesis

Gamma exposure levels, computed pre-open from open interest that settled the
prior night, mark **where** to trade. EMA alignment, pattern, and volume confirm
**when**. Levels are the core; the rest is confirmation.

Read [`HANDOFF.md`](HANDOFF.md) before you trust it with money. It documents
what has actually been measured, what hasn't, and where the evidence is thin —
including the fact that the gamma layer itself has never been validated on
historical data.

---

## What runs

| File | Role |
|---|---|
| `strategy.py` | The 13-check framework. **Single source of truth for every rule.** |
| `gexbot.py` | The daemon. Fetches data, calls `evaluate()`, places and manages orders. |
| `dashboard.py` | Live UI on :8787. Token auth; refuses a public bind without one. |
| `watchdog.py` | Heartbeat monitor. Alerts if the bot goes quiet during market hours. |
| `gexrecon.py` | Rebuilds historical gamma maps from EOD open interest. |
| `selftest.py` | Offline end-to-end verification. No broker, no network. |
| `deploy/` | Installer, systemd units, env template. |

Architecture rule: changing what a trade *is* → edit `strategy.py`. Changing how
data is fetched or orders are placed → edit `gexbot.py`. Never put a threshold
in `gexbot.py`.

## A session

```
08:25   resolve expiries · build the aggregate gamma map · freeze zones · daily bias
08:35   scan every 30s — each bar runs all 13 checks
A+      open a debit spread, direction from the EMA stack
holding manage every 10s → target +90% / stop −45% / force-flat (0DTE only)
always  journal every evaluation, passing or failing
```

No time-based exit. Hold to target, stop, or close.

---

## Run it locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

./venv/bin/python selftest.py        # verify the pipeline, offline
./venv/bin/python strategy.py        # print the frozen parameters
./venv/bin/python gexbot.py          # paper mode, dashboard on :8787
```

`selftest.py` exercises indicators, zone construction, expiry selection, all 13
checks, position sizing, the look-ahead guards, the dashboard, and its auth —
against synthetic data, with no broker connection. Run it after every change.

---

## Deploy to a VPS

A $6/mo box is plenty — this is not compute-bound. Ubuntu 22.04+ or Debian 12+.

```bash
scp -r GEX/ root@your-vps:/tmp/gexbot-src
ssh root@your-vps 'bash /tmp/gexbot-src/deploy/install.sh'
```

The installer creates the system user, venv, and `/opt/gexbot`; installs
`tzdata` (required — every timestamp is `America/Chicago`); runs the selftest
and **refuses to enable the service if it fails**; then installs the systemd
unit and a watchdog timer. Re-running it upgrades code in place and never
touches `gexbot.env` or `data/`.

Then, on the box:

1. **Authorize Robinhood.** Complete the MCP OAuth flow on a machine with a
   browser and copy the cached credentials into `/opt/gexbot/data/`, then
   `chown -R gexbot:gexbot /opt/gexbot/data`. A standalone daemon needs its own
   grant — a Claude connection does not transfer.
2. Verify `agentic_allowed: true` and options level 3 via `get_accounts`.
3. Edit `/opt/gexbot/gexbot.env`. Leave `GEXBOT_ARMED` commented.
4. `systemctl start gexbot && journalctl -u gexbot -f`

It restarts on failure, survives reboots, and reconnects on its own if the
broker drops — the supervisor rebuilds the transport with exponential backoff
while the dashboard stays up so you can see *why* it's disconnected.

### Watching it

The dashboard binds `127.0.0.1` by default. Reach it over SSH:

```bash
ssh -L 8787:127.0.0.1:8787 root@your-vps
# then open http://127.0.0.1:8787
```

For phone access, install Tailscale on the VPS and set
`GEXBOT_DASH_HOST=0.0.0.0` **with** a token. `serve()` refuses to bind a
non-loopback address without one. Do not open 8787 to the internet — the page
shows your positions.

```bash
systemctl status gexbot            # is it up
journalctl -u gexbot -f            # what is it doing
journalctl -u gexbot-watchdog -n 20
```

### Alerts

Set `GEXBOT_ALERT_EMAIL` and the SMTP variables in `gexbot.env` and the
watchdog will email you when the heartbeat goes stale during market hours, or
when the daily loss limit halts trading. Without them it logs to the journal
instead. `watchdog.py:alert()` is a single function — swap it for Pushover,
ntfy, or Twilio if you'd rather get a push.

---

## Going live

Live trading requires **both**:

- `GEXBOT_ARMED=yes` in the environment, and
- `--live` on the command line.

Two switches, on purpose. There is no third way, and please don't add one.

```bash
# in gexbot.env
GEXBOT_ARMED=yes
# in the systemd unit
ExecStart=/opt/gexbot/venv/bin/python /opt/gexbot/gexbot.py --live
```

Before pointing this at a funded account:

- Paper-trade it for at least two weeks and read the journal. The checklist
  fires on roughly 27% of sessions, so a fortnight is a handful of triggers.
- Check Robinhood's current terms on programmatic access through the agentic
  endpoint.
- Re-read the Evidence section of `HANDOFF.md`. Every confidence interval in
  the backtest crosses zero, option prices there were modeled rather than
  quoted, and the gamma layer is untested. `GEXBOT_ACCOUNT_VALUE` drives
  position sizing at 1% risk per trade — set it honestly.

This software places real orders with real money and can lose it. It is not
financial advice, and nothing in the repository establishes that the strategy
has an edge.
