#!/usr/bin/env bash
# Включает мини-приложение по адресу https://<домен>: ставит Caddy (сам получает
# и продлевает HTTPS-сертификат), направляет домен на мини-приложение бота и
# записывает адрес в /root/maxbot/.env.
#   curl -sL https://raw.githubusercontent.com/rzaliev-rin/Aliev/claude/bot-metrika-refactor-4t2oii/deploy/set_domain.sh | bash
set -e
ENV=/root/maxbot/.env

printf "\nВведите домен мини-приложения (например app.example.ru), затем Ввод:\n> " > /dev/tty
IFS= read -r DOMAIN < /dev/tty
DOMAIN=$(echo "$DOMAIN" | tr -d '[:space:]' | sed -e 's|^https\?://||' -e 's|/.*$||' | tr 'A-Z' 'a-z')
[ -n "$DOMAIN" ] || { echo "Домен не указан."; exit 1; }

MYIP=$(curl -s4 --max-time 10 https://ifconfig.me || true)
DNSIP=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}')
echo "IP сервера: ${MYIP:-?}   Домен $DOMAIN указывает на: ${DNSIP:-не найден}"
if [ -z "$DNSIP" ] || { [ -n "$MYIP" ] && [ "$DNSIP" != "$MYIP" ]; }; then
  echo
  echo "⚠️ Домен ещё не направлен на этот сервер. В настройках DNS домена создайте запись:"
  echo "   тип A, имя @ (или поддомен), значение ${MYIP:-IP сервера}"
  echo "Изменения DNS расходятся от 15 минут до нескольких часов — потом запустите команду снова."
  exit 1
fi

echo "=== Ставлю Caddy (веб-сервер с автоматическим HTTPS)…"
if ! command -v caddy > /dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg > /dev/null
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy > /dev/null
fi

PORT=$(grep -E '^WEBAPP_PORT=' "$ENV" | cut -d= -f2)
PORT=${PORT:-8080}
cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
}
CADDY

if command -v ufw > /dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp > /dev/null; ufw allow 443/tcp > /dev/null
fi

grep -v '^WEBAPP_URL=' "$ENV" > "$ENV.tmp" || true
echo "WEBAPP_URL=https://$DOMAIN" >> "$ENV.tmp"
cat "$ENV.tmp" > "$ENV"; rm -f "$ENV.tmp"

systemctl restart maxbot
systemctl reload caddy || systemctl restart caddy
echo "Жду сертификат HTTPS (до минуты)…"
for i in $(seq 1 12); do
  sleep 5
  if curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN/" | grep -q "^200$"; then
    echo
    echo "ГОТОВО: мини-приложение открывается по адресу https://$DOMAIN"
    echo "Теперь укажите этот адрес в business.max.ru (см. инструкцию)."
    exit 0
  fi
done
echo
echo "ОШИБКА: https://$DOMAIN пока не открывается. Последние строки журнала:"
journalctl -u caddy -n 15 --no-pager
