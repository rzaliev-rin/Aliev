#!/bin/bash
# Запуск «Маркетинг ПИ»: склад MaxPoster -> Вордстат -> план в Google Таблицу.
# Запуск из папки бота:  bash marketing_run.sh
cd "$(dirname "$0")"
[ -f venv/bin/activate ] && source venv/bin/activate
echo "=== Проверка программы ==="
python3 tests/test_marketing.py > /dev/null || { echo "ОШИБКА: проверка не прошла, пришлите этот текст Claude"; exit 1; }
echo "Проверка пройдена."
echo "=== Строю маркетинговый план ==="
python3 -m marketing --sheet "$@"
