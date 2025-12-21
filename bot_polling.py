import os
import json
import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))

# твой GitHub Pages URL
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://aerlaedt-netizen.github.io/eviknumber2/")

dp = Dispatcher()

@dp.message(F.text == "/start")
async def start(message: Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини‑апп", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )
    await message.answer("Нажми кнопку, откроется мини‑апп.", reply_markup=kb)

@dp.message(F.web_app_data)
async def webapp_data_handler(message: Message):
    raw = message.web_app_data.data  # строка, которую отправил tg.sendData(...)
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}

    u = message.from_user
    text = (
        "Данные из Mini App:\n"
        f"От: {u.full_name} (@{u.username or 'нет'}) id={u.id}\n"
        f"Payload: {json.dumps(data, ensure_ascii=False)}"
    )

    # Важно: TARGET_USER_ID должен быть пользователем, который уже нажал /start у бота
    await message.bot.send_message(TARGET_USER_ID, text)
    await message.answer("Ок, отправил данные получателю в ЛС.")

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")
    if not TARGET_USER_ID:
        raise RuntimeError("Не задан TARGET_USER_ID (или равен 0)")

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
