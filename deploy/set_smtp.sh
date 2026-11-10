#!/usr/bin/env bash
# Включает вход по корпоративной почте: записывает в /root/maxbot/.env настройки
# SMTP-ящика (их выдаёт IT), отправляет пробное письмо и перезапускает бота.
#   curl -sL https://raw.githubusercontent.com/rzaliev-rin/Aliev/claude/bot-metrika-refactor-4t2oii/deploy/set_smtp.sh | bash
set -e
ENV=/root/maxbot/.env

ask() {
  local answer
  printf "\n%s\n> " "$1" > /dev/tty
  IFS= read -r answer < /dev/tty
  printf -v "$2" "%s" "$(echo "$answer" | tr -d '[:space:]')"
}
setenv() {  # setenv ИМЯ значение — заменить строку в .env или дописать
  local tmp
  tmp=$(mktemp)
  grep -v "^$1=" "$ENV" > "$tmp" || true
  printf "%s=%s\n" "$1" "$2" >> "$tmp"
  cat "$tmp" > "$ENV"
  rm -f "$tmp"
}

ask "Адрес SMTP-сервера (например smtp.expocar.ru):" HOST
ask "Порт (465 или 587; просто Ввод — 465):" PORT
PORT=${PORT:-465}
ask "Логин ящика (обычно сам адрес, например bot@expocar.ru):" SUSER
printf "\nПароль ящика (при вводе не отображается), затем Ввод:\n> " > /dev/tty
IFS= read -rs PASS < /dev/tty; echo > /dev/tty
ask "Адрес отправителя (просто Ввод — как логин):" FROM
FROM=${FROM:-$SUSER}
ask "Ваша рабочая почта для пробного письма:" TEST_TO
if [ "$PORT" = "465" ]; then SEC=ssl; else SEC=starttls; fi

echo "Отправляю пробное письмо…"
if SMTP_HOST="$HOST" SMTP_PORT="$PORT" SMTP_SECURITY="$SEC" SMTP_USER="$SUSER" SMTP_PASSWORD="$PASS" \
   SMTP_FROM="$FROM" TEST_TO="$TEST_TO" /root/maxbot/venv/bin/python - <<'PY'
import os, smtplib, ssl
from email.message import EmailMessage
m = EmailMessage()
m["Subject"] = "Проверка: бот «Метрика_оценка АСП» может отправлять коды"
m["From"] = os.environ["SMTP_FROM"]
m["To"] = os.environ["TEST_TO"]
m.set_content("Если вы видите это письмо — вход в бота по корпоративной почте настроен.")
host, port, sec = os.environ["SMTP_HOST"], int(os.environ["SMTP_PORT"]), os.environ["SMTP_SECURITY"]
ctx = ssl.create_default_context()
s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) if sec == "ssl" else smtplib.SMTP(host, port, timeout=30)
if sec == "starttls":
    s.starttls(context=ctx)
if os.environ["SMTP_USER"]:
    s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
s.send_message(m)
s.quit()
PY
then
  setenv SMTP_HOST "$HOST"; setenv SMTP_PORT "$PORT"; setenv SMTP_SECURITY "$SEC"
  setenv SMTP_USER "$SUSER"; setenv SMTP_PASSWORD "'$PASS'"; setenv SMTP_FROM "$FROM"
  unset PASS
  systemctl restart maxbot
  echo
  echo "ГОТОВО: письмо отправлено на $TEST_TO, вход по корпоративной почте включён."
else
  unset PASS
  echo
  echo "ОШИБКА: письмо не ушло, настройки НЕ сохранены. Сделайте скриншот и пришлите Claude."
fi
