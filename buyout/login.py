# -*- coding: utf-8 -*-
"""
Разовый вход в аккаунт площадки — сохраняет сессию браузера в файл.

Запускать на компьютере с экраном (откроется окно браузера):
    python -m buyout.login avito tmn-avito-1
Войдите в аккаунт (логин, SMS), затем вернитесь в терминал и нажмите Enter.
Рядом появится файл buyout_session_avito_tmn-avito-1.json — пришлите его боту
«Выкуп и комиссия» в MAX (владелец или администратор), бот сохранит вход сам.
"""
import asyncio
import os
import re
import shutil
import sys

from buyout.platforms.browser import SELECTORS, session_path

LOGIN_URLS = {
    "avito": "https://www.avito.ru/#login",
    "autoru": "https://auto.ru/",
    "drom": "https://my.drom.ru/sign",
}


async def main(platform: str, account: str) -> None:
    from playwright.async_api import async_playwright
    path = session_path(platform, account)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)
        ctx = await browser.new_context(locale="ru-RU", **({"storage_state": path} if os.path.exists(path) else {}))
        page = await ctx.new_page()
        await page.goto(LOGIN_URLS[platform])
        await asyncio.get_event_loop().run_in_executor(
            None, input, "Войдите в аккаунт в открывшемся окне и нажмите Enter здесь… ")
        await ctx.storage_state(path=path)
        await browser.close()
    copy = os.path.abspath(f"buyout_session_{platform}_{account}.json")
    shutil.copyfile(path, copy)
    print(f"Вход сохранён: {path}")
    print(f"Пришлите боту «Выкуп и комиссия» в MAX этот файл: {copy}")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in SELECTORS or not re.fullmatch(r"[a-z0-9_\-]{1,40}", sys.argv[2]):
        print("Использование: python -m buyout.login <avito|autoru|drom> <имя_аккаунта>\n"
              "Имя аккаунта — латиница/цифры/«-», например tmn-drom-1")
        sys.exit(1)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
