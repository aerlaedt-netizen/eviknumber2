import os
import json
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL")  # https://aerlaedt-netizen.github.io/eviknumber2/

dp = Dispatcher()


def _dt(ts_ms: int | None) -> str:
    if not ts_ms:
        return "—"
    try:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _clean(s: str | None) -> str:
    s = (s or "").strip()
    return s if s else "—"


def _maps_link_from_geo(geo_text: str | None) -> str | None:
    """
    geo_text ожидаем вида: "55.7558, 37.6173"
    """
    if not geo_text:
        return None
    t = geo_text.replace(" ", "")
    if "," not in t:
        return None
    lat, lon = t.split(",", 1)
    try:
        float(lat); float(lon)
    except Exception:
        return None
    return f"https://maps.google.com/?q={lat},{lon}"


@dp.message(F.text == "/start")
async def start(message: Message):
    if not WEBAPP_URL:
        await message.answer("WEBAPP_URL не задан в переменных окружения.")
        return

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Открыть мини‑апп", web_app=WebAppInfo(url=WEBAPP_URL))]],
        resize_keyboard=True
    )
    await message.answer("Нажмите кнопку и отправьте заявку из мини‑аппа.", reply_markup=kb)


@dp.message(F.web_app_data)
async def webapp_data_handler(message: Message):
    raw = message.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}

    # Под твой текущий payload:
    # {type:"evac_min", phone, address, carBrand, geo, ts}
    phone = _clean(data.get("phone"))
    address = _clean(data.get("address"))
    car_brand = _clean(data.get("carBrand"))
    geo = _clean(data.get("geo"))
    ts = data.get("ts")

    maps_link = _maps_link_from_geo(data.get("geo"))

    sender = message.from_user
    sender_line = f"{sender.full_name} (id={sender.id}" + (f", @{sender.username}" if sender.username else "") + ")"

    lines = [
        "🛻 Заявка на эвакуатор",
        f"Время: {_dt(ts)}",
        f"Клиент: {sender_line}",
        "",
        f"Телефон: {phone}",
        f"Марка: {car_brand}",
        f"Адрес: {address}",
        f"Гео: {geo}",
    ]
    if maps_link:
        lines.append(f"Карта: {maps_link}")

    text = "\n".join(lines)

    # Отправляем тебе в ЛС
    try:
        await message.bot.send_message(TARGET_USER_ID, text)
        await message.answer("Заявка отправлена диспетчеру.")
    except Exception as e:
        await message.answer("Не смог отправить диспетчеру. Он точно нажал /start у бота?")
        # чтобы видеть ошибку в логах Render:
        raise


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not TARGET_USER_ID:
        raise RuntimeError("TARGET_USER_ID не задан или 0")
    if not WEBAPP_URL:
        raise RuntimeError("WEBAPP_URL не задан")

    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
