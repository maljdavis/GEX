#!/usr/bin/env python3
"""
watchdog — answers "is it actually live?"

Reads the heartbeat file gexbot writes every cycle. If it goes stale during
market hours, something is wrong even if systemd thinks the process is fine
(hung MCP call, deadlock, network black hole).

Run from cron every 5 minutes:
    */5 * * * * /usr/bin/python3 /opt/gexbot/watchdog.py
"""

import json
import os
import smtplib
import sys
from datetime import datetime, time as dtime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
HOME = Path(os.environ.get("GEXBOT_HOME", Path.home() / ".gexbot"))
HEARTBEAT = HOME / "heartbeat"
STATE = HOME / "state.json"
ALERT_STAMP = HOME / ".last_alert"

STALE_SECONDS = 180
MARKET_OPEN, MARKET_CLOSE = dtime(8, 25), dtime(15, 5)
ALERT_COOLDOWN = 900


def in_session(n: datetime) -> bool:
    return n.weekday() < 5 and MARKET_OPEN <= n.time() <= MARKET_CLOSE


def alert(subject: str, body: str):
    """Swap this for Pushover/Twilio/ntfy — email is just the default."""
    to = os.environ.get("GEXBOT_ALERT_EMAIL")
    if not to:
        print(f"ALERT: {subject}\n{body}", file=sys.stderr)
        return
    msg = EmailMessage()
    msg["Subject"] = f"[gexbot] {subject}"
    msg["From"] = os.environ.get("GEXBOT_SMTP_USER", to)
    msg["To"] = to
    msg.set_content(body)
    with smtplib.SMTP_SSL(os.environ.get("GEXBOT_SMTP_HOST", "smtp.gmail.com"), 465) as s:
        s.login(os.environ["GEXBOT_SMTP_USER"], os.environ["GEXBOT_SMTP_PASS"])
        s.send_message(msg)


def throttled() -> bool:
    if not ALERT_STAMP.exists():
        return False
    last = datetime.fromisoformat(ALERT_STAMP.read_text())
    return (datetime.now(TZ) - last).total_seconds() < ALERT_COOLDOWN


def fire(subject: str, body: str):
    if throttled():
        return
    alert(subject, body)
    ALERT_STAMP.write_text(datetime.now(TZ).isoformat())


def main():
    n = datetime.now(TZ)
    if not in_session(n):
        return

    if not HEARTBEAT.exists():
        fire("no heartbeat", "Heartbeat file missing during market hours. Process likely never started.")
        return

    beat = datetime.fromisoformat(HEARTBEAT.read_text().strip())
    age = (n - beat).total_seconds()

    if age > STALE_SECONDS:
        detail = f"Last heartbeat {age:.0f}s ago ({beat:%H:%M:%S CT})."
        if STATE.exists():
            st = json.loads(STATE.read_text())
            if st.get("position"):
                detail += "\n\nA POSITION IS OPEN AND UNMANAGED. Check the broker now."
        fire("heartbeat stale", detail)
        return

    if STATE.exists():
        st = json.loads(STATE.read_text())
        if st.get("halted"):
            fire("halted", f"Trading halted: {st.get('halt_reason')}. Realized today: {st.get('realized_today'):.0f}")

    print(f"ok — heartbeat {age:.0f}s old")


if __name__ == "__main__":
    main()
