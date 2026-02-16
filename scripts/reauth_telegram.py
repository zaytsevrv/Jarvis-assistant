"""
Скрипт переавторизации Telethon.
Запуск: python3 reauth_telegram.py
Используется когда Telegram запрашивает повторный вход.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from telethon import TelegramClient
from src import config


async def main():
    client = TelegramClient(
        "jarvis_session",
        config.TELEGRAM_API_ID,
        config.TELEGRAM_API_HASH,
    )
    await client.start(phone=config.TELEGRAM_PHONE)
    me = await client.get_me()
    print(f"Авторизован как: {me.first_name} {me.last_name or ''} (@{me.username})")
    print("Сессия сохранена. Можно закрывать.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
