#!/usr/bin/env bash
# Установка бота «Выкуп и комиссия» (ИИ-агент) на сервер Ubuntu одной командой:
#   curl -sL https://raw.githubusercontent.com/rzaliev-rin/Aliev/claude/peaceful-dijkstra-2793bo/deploy/install_buyout.sh | bash
# Это ОТДЕЛЬНЫЙ бот: папка /root/buyoutbot, служба buyoutbot. Бота метрики (/root/maxbot, maxbot) не трогает,
# можно ставить на тот же сервер. Повторный запуск безопасен: обновляет код, .env и данные не трогает.
set -e

BRANCH="claude/peaceful-dijkstra-2793bo"
APP=/root/buyoutbot
ZIP_URL="https://codeload.github.com/rzaliev-rin/Aliev/zip/refs/heads/$BRANCH"

ask() {  # ask "Вопрос" ПЕРЕМЕННАЯ — читаем с клавиатуры, даже когда скрипт пришёл через curl | bash
  local answer
  printf "\n%s\n> " "$1" > /dev/tty
  IFS= read -r answer < /dev/tty
  printf -v "$2" "%s" "$(echo "$answer" | tr -d '[:space:]')"
}

echo "=== 1/5 Устанавливаю Python (1–3 минуты)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl unzip ca-certificates > /dev/null

echo "=== 2/5 Скачиваю бота с GitHub…"
TMP=$(mktemp -d)
curl -sL -o "$TMP/bot.zip" "$ZIP_URL"
python3 -m zipfile -e "$TMP/bot.zip" "$TMP/src"
SRC=$(ls -d "$TMP"/src/Aliev-*)
mkdir -p "$APP"
rm -rf "$APP/buyout"
cp -r "$SRC/buyout" "$APP/buyout"
rm -rf "$TMP"

echo "=== 3/5 Ставлю библиотеки и браузер Chromium (3–6 минут)…"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/buyout/requirements.txt"
"$APP/venv/bin/playwright" install --with-deps chromium > /dev/null

echo "=== 4/5 Настройки"
if [ -f "$APP/.env" ]; then
  echo "Файл настроек уже есть — оставляю как есть ($APP/.env)."
else
  ask "Вставьте ТОКЕН НОВОГО бота MAX (от @MasterBot; не токен бота метрики!):" BOT_TOKEN
  while [ -z "$BOT_TOKEN" ]; do ask "Токен обязателен. Вставьте токен бота MAX:" BOT_TOKEN; done
  ask "Ваш ID в MAX — владелец бота (просто Ввод = 4852272):" OWNER
  OWNER=${OWNER:-4852272}
  ask "Ключ Anthropic API (sk-ant-…; или просто Ввод — добавить позже):" AI_KEY
  ask "Адрес вебхука Битрикс24 (https://…/rest/…/…/; или просто Ввод — добавить позже):" BX_URL
  sed -e "s|^BUYOUT_BOT_TOKEN=.*|BUYOUT_BOT_TOKEN=$BOT_TOKEN|" \
      -e "s|^BITRIX_WEBHOOK_URL=.*|BITRIX_WEBHOOK_URL=$BX_URL|" \
      -e "s|^OWNER_USER_ID=.*|OWNER_USER_ID=$OWNER|" \
      -e "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$AI_KEY|" \
      "$APP/buyout/.env.example" > "$APP/.env"
  chmod 600 "$APP/.env"
  echo "Настройки сохранены в $APP/.env (часовой пояс: Екатеринбург/Тюмень — поменять там же, TZ=…)"
fi

echo "=== 5/5 Запускаю бота как службу buyoutbot…"
cat > /etc/systemd/system/buyoutbot.service <<UNIT
[Unit]
Description=MAX bot Buyout and Commission (AI agent)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python -m buyout.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q buyoutbot
systemctl restart buyoutbot
sleep 8

if systemctl is-active -q buyoutbot; then
  echo
  echo "ГОТОВО: бот «Выкуп и комиссия» запущен. Напишите ему /start в MAX."
else
  echo
  echo "ОШИБКА: бот не запустился. Последние строки журнала:"
  journalctl -u buyoutbot -n 25 --no-pager
fi
