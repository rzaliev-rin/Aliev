#!/usr/bin/env bash
# Установка бота «Метрика_оценка АСП» на чистый сервер Ubuntu одной командой:
#   curl -sL https://raw.githubusercontent.com/rzaliev-rin/Aliev/claude/bot-metrika-refactor-4t2oii/deploy/install.sh | bash
# Повторный запуск безопасен: обновляет код, а .env, ключ Google и данные бота не трогает.
set -e

BRANCH="claude/bot-metrika-refactor-4t2oii"
APP=/root/maxbot
ZIP_URL="https://codeload.github.com/rzaliev-rin/Aliev/zip/refs/heads/$BRANCH"

ask() {  # ask "Вопрос" ПЕРЕМЕННАЯ — читаем с клавиатуры, даже когда скрипт пришёл через curl | bash
  local answer
  printf "\n%s\n> " "$1" > /dev/tty
  IFS= read -r answer < /dev/tty
  printf -v "$2" "%s" "$(echo "$answer" | tr -d '[:space:]')"
}

echo "=== 1/5 Устанавливаю Python и нужные программы (1–3 минуты)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip curl unzip ca-certificates > /dev/null

echo "=== 2/5 Скачиваю бота с GitHub…"
TMP=$(mktemp -d)
curl -sL -o "$TMP/bot.zip" "$ZIP_URL"
python3 -m zipfile -e "$TMP/bot.zip" "$TMP/src"
SRC=$(ls -d "$TMP"/src/Aliev-*)
mkdir -p "$APP/tests"
cp "$SRC"/*.py "$SRC"/requirements.txt "$SRC"/params_table_v2.csv "$APP"/
cp "$SRC"/tests/*.py "$APP"/tests/
rm -rf "$TMP"

echo "=== 3/5 Ставлю библиотеки бота…"
[ -d "$APP/venv" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"

echo "=== 4/5 Настройки"
if [ -f "$APP/.env" ]; then
  echo "Файл настроек уже есть — оставляю как есть."
else
  ask "Вставьте ТОКЕН БОТА MAX и нажмите Ввод:" BOT_TOKEN
  while [ -z "$BOT_TOKEN" ]; do ask "Токен обязателен. Вставьте токен бота MAX:" BOT_TOKEN; done
  ask "Вставьте готовый КЛЮЧ MaxPoster API, если он у вас есть (нет — просто Ввод):" MP_KEY
  if [ -z "$MP_KEY" ]; then
    # ключ = base64("email:пароль") служебного аккаунта MaxPoster — соберём его сами
    ask "Email (логин) служебного аккаунта MaxPoster для бота (или Ввод — добавить позже):" MP_LOGIN
    if [ -n "$MP_LOGIN" ]; then
      printf "\nПароль этого аккаунта (при вводе не отображается), затем Ввод:\n> " > /dev/tty
      IFS= read -rs MP_PASS < /dev/tty; echo > /dev/tty
      MP_KEY=$(printf "%s:%s" "$MP_LOGIN" "$MP_PASS" | base64 -w0)
      unset MP_PASS
      CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X POST \
           https://api.maxposter.ru/partners-api/appraisals \
           -H "Authorization: Basic $MP_KEY" -H "Content-Type: application/json" \
           -d '{"limit":1,"offset":0,"orders":["-id"]}' || true)
      case "$CODE" in
        200) echo "MaxPoster: доступ проверен ✓" ;;
        401|403) echo "⚠️ MaxPoster не пустил этот аккаунт (код $CODE) — бот запустится без данных MaxPoster. Напишите Claude." ;;
        *) echo "MaxPoster: проверить не удалось (код $CODE), ключ сохранён — проверим по первой оценке." ;;
      esac
    fi
  fi
  ask "Вставьте ID Google Таблицы (или просто Ввод — добавить позже):" SHEET_ID
  cat > "$APP/.env" <<ENV
MAX_BOT_TOKEN=$BOT_TOKEN
MAXPOSTER_API_KEY=$MP_KEY
MAXPOSTER_API_BASE=https://api.maxposter.ru
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_SHEET_ID=$SHEET_ID
OWNER_USER_ID=4852272
ENV
  chmod 600 "$APP/.env"
  echo "Настройки сохранены в $APP/.env"
fi

echo "=== 5/5 Запускаю бота как службу (сам поднимается после перезагрузки и сбоев)…"
cat > /etc/systemd/system/maxbot.service <<UNIT
[Unit]
Description=MAX bot Metrika ASP
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$APP
ExecStart=$APP/venv/bin/python $APP/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q maxbot
systemctl restart maxbot
sleep 8

if systemctl is-active -q maxbot; then
  echo
  echo "ГОТОВО: бот запущен. Напишите ему /start в MAX."
else
  echo
  echo "ОШИБКА: бот не запустился. Последние строки журнала:"
  journalctl -u maxbot -n 25 --no-pager
fi
