#!/bin/sh
# Installs the bot on an Ubuntu or Debian server. Clone the repo to /opt/mikasa and run this as root.
set -e

cd /opt/mikasa
apt-get update
apt-get install -y python3-venv

id mikasa >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin mikasa

python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Never overwrites existing credentials.
if [ ! -f .env ]; then
    printf 'DISCORD_TOKEN=\nTWITCH_CLIENT_ID=\nTWITCH_CLIENT_SECRET=\n' > .env
fi

chown -R mikasa:mikasa /opt/mikasa
chmod 600 .env

cp mikasa.service /etc/systemd/system/mikasa.service
systemctl daemon-reload
systemctl enable mikasa

echo
echo "Installed. Next: nano /opt/mikasa/.env"
echo "Then: systemctl start mikasa"
