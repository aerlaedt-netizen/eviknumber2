import os
import json
import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from aiohttp import web

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL")              # GitHub Pages
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL")    # Render Web Service URL, напр. https://xxx.onrender.com
PORT = int(os.getenv("PORT", "10000"))

dp = Dispatcher()

greeted_users: set[int] = set()
last_request_ts: dict[int, float] = {}
COOLDOWN_SECONDS = 5 * 60

STATE_FILE = os.getenv("STATE_FILE", "bot_state.json")
drivers_on_line: int = 0


def is_dispatcher(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id == TARGET_USER_ID


def load_state() -> None:
    global drivers_on_line
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        drivers_on_line = int(st.get("drivers_on_line", 0))
        if drivers_on_line < 0:
            drivers_on_line = 0
    except Exception:
        drivers_on_line = 0


def save_state() -> None:
    st = {"drivers_on_line": drivers_on_line}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def with_query(url: str, **params) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    for k, v in params.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = str(v)
    new_query = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_query, p.fragment))


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


def _yandex_maps_link_from_geo(geo_text: str | None) -> str | None:
    if not geo_text:
        return None
    t = geo_text.replace(" ", "")
    if "," not in t:
        return None
    lat_s, lon_s = t.split(",", 1)
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except Exception:
        return None
    return f"https://yandex.ru/maps/?pt={lon},{lat}&z=16&l=map"


# ===== Ваш стартовый текст (добавлен в код) =====
START_TEXT = """Ваш надежный помощник в любой ситуации на дороге — бот службы эвакуации!

Застряли на дороге? Автомобиль сломался или попал в аварию? Не тратьте время на поиски эвакуатора — наш бот сделает всё за вас!

С помощью нашего удобного сервиса вы сможете:
 • Быстро вызвать эвакуатор в любой точке города или за его пределами.
 • Получить точную информацию о времени прибытия и стоимости услуги.
 • Выбрать подходящий тип эвакуатора для вашего автомобиля.

Почему выбирают нас?
 • Круглосуточная работа 24/7.
 • Быстрая обработка запросов через бота.
 • Надежные и проверенные водители эвакуаторов.
 • Прозрачные цены без скрытых платежей.

Нажмите кнопку ниже, заполните форму — заявка придёт диспетчеру.
"""


# =========================
# API (aiohttp) для Mini App
# =========================

@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def api_drivers(request: web.Request) -> web.Response:
    return web.json_response({"drivers_on_line": drivers_on_line, "updated_at": int(time.time())})


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def start_http_server() -> web.AppRunner:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/api/drivers", api_drivers)
    app.router.add_get("/healthz", healthz)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    return runner


# =========================
# Команды диспетчера (только TARGET_USER_ID)
# =========================

@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")
        return

    text = (
        "Команды диспетчера:\n"
        "/help — показать команды\n"
        "/drivers — текущее количество водителей на линии\n"
        "/setdrivers <n> — установить количество\n"
        "/adddrivers <n> — прибавить\n"
        "/deldrivers <n> — убавить (не ниже 0)\n"
    )
    await message.answer(text)


@dp.message(Command("drivers"))
async def drivers_cmd(message: Message) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")
        return
    await message.answer(f"Водителей на линии сейчас: {drivers_on_line}")


@dp.message(Command("setdrivers"))
async def setdrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /setdrivers <число>\nНапример: /setdrivers 5")
        return

    try:
        n = int(arg)
        if n < 0:
            raise ValueError
    except Exception:
        await message.answer("Нужно целое число ≥ 0.\nНапример: /setdrivers 3")
        return

    global drivers_on_line
    drivers_on_line = n
    save_state()
    await message.answer(f"Готово. Водителей на линии теперь: {drivers_on_line}")


@dp.message(Command("adddrivers"))
async def adddrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /adddrivers <число>\nНапример: /adddrivers 2")
        return

    try:
        delta = int(arg)
        if delta < 0:
            raise ValueError
    except Exception:
        await message.answer("Нужно целое число ≥ 0.\nНапример: /adddrivers 1")
        return

    global drivers_on_line
    drivers_on_line += delta
    save_state()
    await message.answer(f"Готово. Водителей на линии теперь: {drivers_on_line}")


@dp.message(Command("deldrivers"))
async def deldrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")
        return

    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /deldrivers <число>\nНапример: /deldrivers 1")
        return

    try:
        delta = int(arg)
        if delta < 0:
            raise ValueError
    except Exception:
        await message.answer("Нужно целое число ≥ 0.\nНапример: /deldrivers 1")
        return

    global drivers_on_line
    drivers_on_line = max(0, drivers_on_line - delta)
    save_state()
    await message.answer(f"Готово. Водителей на линии теперь: {drivers_on_line}")


# =========================
# /start
# =========================

@dp.message(Command("start"))
async def start(message: Message) -> None:
    if not WEBAPP_URL:
        await message.answer("WEBAPP_URL не задан.")
        return
    if not PUBLIC_BASE_URL:
        await message.answer("PUBLIC_BASE_URL не задан.")
        return

    uid = message.from_user.id

    # Приветствие показываем один раз (до перезапуска процесса)
    if uid not in greeted_users:
        greeted_users.add(uid)
        await message.answer(START_TEXT)

    api_url = PUBLIC_BASE_URL.rstrip("/") + "/api/drivers"
    webapp_url = with_query(WEBAPP_URL, drivers=drivers_on_line, api=api_url)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Заказать эвакуатор", web_app=WebAppInfo(url=webapp_url))]],
        resize_keyboard=True,
    )

    await message.answer("Откройте мини‑апп и отправьте заявку.", reply_markup=kb)


# =========================
# Приём заявки из Mini App
# =========================

@dp.message(F.web_app_data)
async def webapp_data_handler(message: Message) -> None:
    uid = message.from_user.id
    now = time.time()

    last = last_request_ts.get(uid)
    if last is not None and (now - last) < COOLDOWN_SECONDS:
        remain = int(COOLDOWN_SECONDS - (now - last))
        mins = remain // 60
        secs = remain % 60
        await message.answer(
            "Заявку можно отправлять не чаще 1 раза в 5 минут.\n"
            f"Попробуйте через {mins:02d}:{secs:02d}."
        )
        return

    raw = message.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}

    phone = _clean(data.get("phoneFormatted") or data.get("phone"))
    address = _clean(data.get("address"))
    car_brand = _clean(data.get("carBrand"))
    geo = _clean(data.get("geo"))
    ts = data.get("ts")

    yandex_link = _yandex_maps_link_from_geo(data.get("geo"))

    sender = message.from_user
    sender_line = (
        f"{sender.full_name} (id={sender.id}"
        + (f", @{sender.username}" if sender.username else "")
        + ")"
    )

    lines = [
        "Заявка на эвакуатор",
        "",
        f"Время: {_dt(ts)}",
        f"Клиент: {sender_line}",
        f"Телефон: {phone}",
        f"Марка: {car_brand}",
        f"Адрес: {address}",
        f"Гео: {geo}",
        f"Водителей на линии (по боту): {drivers_on_line}",
    ]
    if yandex_link:
        lines.append(f"Яндекс.Карты: {yandex_link}")

    await message.bot.send_message(TARGET_USER_ID, "\n".join(lines))

    last_request_ts[uid] = now
    await message.answer("Заявка отправлена, ожидайте, с вами свяжется диспетчер, обычно до 10 минут")


# =========================
# “Скрыть команды” для всех кроме диспетчера
# (ставим в конце, чтобы не мешал конкретным командам выше)
# =========================

@dp.message(F.text.startswith("/"))
async def block_commands_for_non_dispatcher(message: Message) -> None:
    if not is_dispatcher(message):
        await message.answer("Команды недоступны.")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not TARGET_USER_ID:
        raise RuntimeError("TARGET_USER_ID не задан или 0")
    if not WEBAPP_URL:
        raise RuntimeError("WEBAPP_URL не задан")
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL не задан")

    load_state()

    runner = await start_http_server()

    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
