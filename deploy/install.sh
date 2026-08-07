#!/usr/bin/env bash
# gexbot VPS installer. Ubuntu 22.04+ / Debian 12+.
#
#   scp -r GEX/ root@your-vps:/tmp/gexbot-src
#   ssh root@your-vps 'bash /tmp/gexbot-src/deploy/install.sh'
#
# Idempotent — safe to re-run to upgrade code in place. Your gexbot.env and
# everything under data/ (credentials, journal, state) are never overwritten.
set -euo pipefail

APP_USER=gexbot
APP_DIR=/opt/gexbot
DATA_DIR=$APP_DIR/data

# Where this script lives, so the installer works from any source directory
# instead of assuming the code is already at its destination.
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

[[ $EUID -eq 0 ]] || { echo "run as root: sudo bash $0" >&2; exit 1; }

echo "==> installing from $SRC_DIR"

echo "==> user + directories"
id -u $APP_USER &>/dev/null || \
  useradd --system --create-home --shell /usr/sbin/nologin $APP_USER
mkdir -p "$APP_DIR" "$DATA_DIR"

echo "==> system packages"
apt-get update -qq
# tzdata is NOT optional: every timestamp in this bot is America/Chicago and
# ZoneInfo raises ZoneInfoNotFoundError without the system tz database. On a
# minimal cloud image it is frequently absent.
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-venv python3-pip tzdata ca-certificates >/dev/null

echo "==> application files"
for f in gexbot.py strategy.py dashboard.py auth.py watchdog.py gexrecon.py \
         selftest.py requirements.txt; do
  install -m 0644 "$SRC_DIR/$f" "$APP_DIR/$f"
done
mkdir -p "$APP_DIR/deploy"
install -m 0644 "$SRC_DIR/deploy/gexbot.env.example" "$APP_DIR/deploy/"

echo "==> python venv"
[[ -d $APP_DIR/venv ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "==> environment file"
if [[ ! -f $APP_DIR/gexbot.env ]]; then
  cp "$SRC_DIR/deploy/gexbot.env.example" "$APP_DIR/gexbot.env"
  TOKEN=$(openssl rand -hex 20)
  sed -i "s|^GEXBOT_DASH_TOKEN=.*|GEXBOT_DASH_TOKEN=$TOKEN|" "$APP_DIR/gexbot.env"
  echo "    generated dashboard token: $TOKEN"
else
  echo "    keeping existing gexbot.env"
fi
chmod 600 "$APP_DIR/gexbot.env"
chown -R $APP_USER:$APP_USER "$APP_DIR"

echo "==> credential migration"
# Early builds ran --login without the env file, so GEXBOT_HOME was unset and
# tokens landed in the service user's home while the daemon looked in
# $DATA_DIR — an endless "not authorized" despite a successful browser flow.
# gexbot.py now reads gexbot.env itself; move any stranded credentials so
# nobody has to authorize twice.
STRANDED=/home/$APP_USER/.gexbot/oauth
if [[ -d $STRANDED ]] && compgen -G "$STRANDED/*.json" >/dev/null; then
  mkdir -p "$DATA_DIR/oauth"
  for f in "$STRANDED"/*.json; do
    [[ -f $DATA_DIR/oauth/$(basename "$f") ]] || mv "$f" "$DATA_DIR/oauth/"
  done
  chown -R $APP_USER:$APP_USER "$DATA_DIR/oauth"
  chmod 700 "$DATA_DIR/oauth"; chmod 600 "$DATA_DIR"/oauth/*.json 2>/dev/null || true
  rmdir "$STRANDED" 2>/dev/null || true
  echo "    moved existing credentials into $DATA_DIR/oauth"
else
  echo "    nothing to migrate"
fi

echo "==> verifying the install before enabling anything"
# Runs offline against synthetic data. If the pipeline is broken, find out
# now rather than at 08:35 with real money on the line.
sudo -u $APP_USER "$APP_DIR/venv/bin/python" "$APP_DIR/selftest.py" >/dev/null || {
  echo "    SELFTEST FAILED — not enabling the service. Run it by hand:" >&2
  echo "    sudo -u $APP_USER $APP_DIR/venv/bin/python $APP_DIR/selftest.py" >&2
  exit 1
}
echo "    selftest passed"

echo "==> systemd"
install -m 0644 "$SRC_DIR/deploy/gexbot.service" /etc/systemd/system/
install -m 0644 "$SRC_DIR/deploy/gexbot-watchdog.service" /etc/systemd/system/
install -m 0644 "$SRC_DIR/deploy/gexbot-watchdog.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable gexbot >/dev/null
systemctl enable --now gexbot-watchdog.timer >/dev/null

# Supersedes the cron line older installs used; the timer replaces it.
rm -f /etc/cron.d/gexbot-watchdog

cat <<DONE

Installed to $APP_DIR. It is NOT running yet, and it is NOT armed.

  1. Authorize Robinhood — once, if --auth-status says you aren't:

        sudo -u $APP_USER $APP_DIR/venv/bin/python $APP_DIR/gexbot.py --auth-status

     From your LAPTOP, forward the callback port:

        ssh -L 8788:127.0.0.1:8788 root@this-host

     then in that session:

        sudo -u $APP_USER $APP_DIR/venv/bin/python $APP_DIR/gexbot.py --login

     It prints a URL. Open it in your laptop browser and approve. Tokens are
     written to $DATA_DIR/oauth and never leave this box.
     Check any time with:  gexbot.py --auth-status

  2. Edit $APP_DIR/gexbot.env — set GEXBOT_ACCOUNT (printed by --login),
     GEXBOT_ACCOUNT_VALUE, and symbols.
     Leave GEXBOT_ARMED commented out. It starts in PAPER mode.

  3. systemctl start gexbot && journalctl -u gexbot -f

  4. Dashboard is on 127.0.0.1:8787. Reach it with an SSH tunnel:
        ssh -L 8787:127.0.0.1:8787 root@this-host
     then open http://127.0.0.1:8787
     Do NOT open port 8787 to the public internet — it shows your positions.

Paper-trade it for a couple of weeks before you even think about arming it.

DONE
