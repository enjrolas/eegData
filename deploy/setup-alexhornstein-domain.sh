#!/bin/bash
# Make eegdata.alexhornstein.com the primary domain for the eegdata site and
# redirect eegdata.artiswrong.com to it.  Run as root:
#     sudo bash /home/japhy/eegdata/deploy/setup-alexhornstein-domain.sh
#
# Every step runs `apache2ctl configtest` before reloading; if a test fails the
# previous config files are restored and the script stops.  Backups of the
# current vhost files go to /home/japhy/backups/apache-eegdata-<timestamp>/.
set -euo pipefail

SRC=/home/japhy/eegdata/deploy
AV=/etc/apache2/sites-available
NEW=eegdata.alexhornstein.com
OLD=eegdata.artiswrong.com
BK=/home/japhy/backups/apache-eegdata-$(date +%Y%m%d-%H%M%S)

[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }
mkdir -p "$BK"
cp -p "$AV/$OLD.conf" "$AV/$OLD-le-ssl.conf" "$BK/"
[ -e "$AV/$NEW.conf" ] && cp -p "$AV/$NEW.conf" "$BK/" || true
[ -e "$AV/$NEW-le-ssl.conf" ] && cp -p "$AV/$NEW-le-ssl.conf" "$BK/" || true
echo "backed up current vhosts to $BK"

restore() {
  echo "!! configtest failed — restoring previous files"
  cp -p "$BK/$OLD.conf" "$BK/$OLD-le-ssl.conf" "$AV/"
  if [ -e "$BK/$NEW.conf" ]; then cp -p "$BK/$NEW.conf" "$AV/"; else a2dissite -q "$NEW" || true; rm -f "$AV/$NEW.conf"; fi
  if [ -e "$BK/$NEW-le-ssl.conf" ]; then cp -p "$BK/$NEW-le-ssl.conf" "$AV/"; else a2dissite -q "$NEW-le-ssl" || true; rm -f "$AV/$NEW-le-ssl.conf"; fi
  apache2ctl configtest && systemctl reload apache2
  exit 1
}
check_reload() { apache2ctl configtest || restore; systemctl reload apache2; sleep 2; }

echo "== 1/4 HTTP vhost for $NEW (needed for the certificate challenge)"
install -m 644 "$SRC/$NEW.conf" "$AV/$NEW.conf"
a2ensite -q "$NEW"
check_reload

echo "== 2/4 certificate for $NEW"
if [ ! -e "/etc/letsencrypt/live/$NEW/fullchain.pem" ]; then
  certbot certonly --apache -d "$NEW"
fi
[ -e "/etc/letsencrypt/live/$NEW/fullchain.pem" ] || { echo "no certificate — stopping (old domain untouched)"; exit 1; }

echo "== 3/4 HTTPS vhost for $NEW (serves the Django app)"
install -m 644 "$SRC/$NEW-le-ssl.conf" "$AV/$NEW-le-ssl.conf"
a2ensite -q "$NEW-le-ssl"
check_reload
code=$(curl -s -o /dev/null -w '%{http_code}' "https://$NEW/")
echo "https://$NEW/ -> $code"
[ "$code" = "200" ] || { echo "new domain not answering 200 — stopping before touching $OLD"; exit 1; }

echo "== 4/4 redirect $OLD -> $NEW"
install -m 644 "$SRC/$OLD.conf" "$AV/$OLD.conf"
install -m 644 "$SRC/$OLD-le-ssl.conf" "$AV/$OLD-le-ssl.conf"
check_reload
curl -s -o /dev/null -w "https://$OLD/analyses -> %{http_code} %{redirect_url}\n" "https://$OLD/analyses"
curl -s -o /dev/null -w "http://$NEW/ -> %{http_code} %{redirect_url}\n" "http://$NEW/"
echo "done. Old vhost files are in $BK"
