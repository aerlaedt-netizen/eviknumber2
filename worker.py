import os
import json
import time
import re
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import asyncpg
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
)
from aiogram.exceptions import TelegramBadRequest

BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL")          # GitHub Pages
API_BASE_URL = os.getenv("API_BASE_URL")      # https://<api-service>.onrender.com
API_ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

dp = Dispatcher()

greeted_users: set[int] = set()
last_request_ts: dict[int, float] = {}
COOLDOWN_SECONDS = 5 * 60

DB_POOL: asyncpg.Pool | None = None
HTTP: aiohttp.ClientSession | None = None

ALLOWED_STATUSES = {"new", "in_work", "done", "cancel"}

MANAGER_AWAIT: dict[int, str] = {}   # user_id -> "setdrivers"

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


def is_dispatcher_user_id(user_id: int) -> bool:
    return user_id == TARGET_USER_ID


def is_dispatcher(message: Message) -> bool:
    return bool(message.from_user) and is_dispatcher_user_id(message.from_user.id)


def with_query(url: str, **params) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    for k, v in params.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = str(v)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(q, doseq=True), p.fragment))


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


def _user_tag_from_row(r: dict) -> str:
    if r.get("tg_username"):
        return f"@{r['tg_username']}"
    return r.get("tg_full_name") or "—"


# ---------------- DB (requests/statuses) ----------------

async def db_init():
    global DB_POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")
    DB_POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with DB_POOL.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
          id           BIGSERIAL PRIMARY KEY,
          created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

          tg_user_id   BIGINT,
          tg_username  TEXT,
          tg_full_name TEXT,

          phone            TEXT,
          phone_formatted  TEXT,
          car_brand        TEXT,
          address          TEXT,
          geo              TEXT,
          yandex_link      TEXT,

          payload_json  JSONB,
          status        TEXT NOT NULL DEFAULT 'new'
        );
        """)
        await con.execute("CREATE INDEX IF NOT EXISTS requests_created_at_idx ON requests (created_at DESC);")
        await con.execute("CREATE INDEX IF NOT EXISTS requests_status_idx ON requests (status);")


async def db_close():
    global DB_POOL
    if DB_POOL:
        await DB_POOL.close()
        DB_POOL = None


async def db_create_request(*, u, payload: dict) -> int:
    yandex_link = _yandex_maps_link_from_geo(payload.get("geo"))
    async with DB_POOL.acquire() as con:
        row = await con.fetchrow("""
            INSERT INTO requests(
              tg_user_id, tg_username, tg_full_name,
              phone, phone_formatted, car_brand, address, geo, yandex_link,
              payload_json
            )
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            RETURNING id
        """,
        u.id,
        u.username,
        u.full_name,
        payload.get("phone"),
        payload.get("phoneFormatted"),
        payload.get("carBrand"),
        payload.get("address"),
        payload.get("geo"),
        yandex_link,
        json.dumps(payload, ensure_ascii=False)
        )
        return int(row["id"])


async def db_get_request(req_id: int) -> dict | None:
    async with DB_POOL.acquire() as con:
        row = await con.fetchrow("SELECT * FROM requests WHERE id=$1", req_id)
        return dict(row) if row else None


async def db_list_requests(limit: int = 10) -> list[dict]:
    limit = max(1, min(50, int(limit)))
    async with DB_POOL.acquire() as con:
        rows = await con.fetch(
            """
            SELECT
              id, created_at, status,
              tg_username, tg_full_name,
              phone_formatted, car_brand, address,
              yandex_link, geo
            FROM requests
            ORDER BY created_at DESC
            LIMIT $1
            """,
            limit
        )
        return [dict(r) for r in rows]


async def db_set_status(req_id: int, status: str) -> bool:
    async with DB_POOL.acquire() as con:
        res = await con.execute("UPDATE requests SET status=$2 WHERE id=$1", req_id, status)
        return res.split()[-1] != "0"


# ---------------- HTTP (to API service) ----------------

async def http_init():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))


async def http_close():
    global HTTP
    if HTTP:
        await HTTP.close()
        HTTP = None


async def api_get_drivers() -> int:
    url = API_BASE_URL.rstrip("/") + "/api/drivers"
    async with HTTP.get(url) as r:
        j = await r.json()
        return int(j.get("drivers_on_line", 0))


async def api_set_drivers(n: int) -> int:
    # ВАЖНО: бот ходит в bot-endpoint, авторизация через X-Admin-Token
    url = API_BASE_URL.rstrip("/") + "/api/bot/drivers"
    headers = {"X-Admin-Token": API_ADMIN_TOKEN or ""}

    async with HTTP.post(url, json={"drivers_on_line": int(n)}, headers=headers) as r:
        text = await r.text()
        try:
            j = json.loads(text)
        except Exception:
            j = {"raw": text}

        if r.status != 200:
            raise RuntimeError(f"API error {r.status}: {j}")

        return int(j.get("drivers_on_line", 0))


# ---------------- UI helpers ----------------

def build_manager_panel_text(drivers: int | None = None) -> str:
    lines = ["Панель менеджера"]
    if drivers is not None:
        lines.append(f"Водителей на линии: {drivers}")
    lines.append("")
    lines.append("Выберите действие кнопками ниже.")
    return "\n".join(lines)


def build_manager_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Заявки (последние 10)", callback_data="panel:req:10"),
                InlineKeyboardButton(text="Заявки (последние 20)", callback_data="panel:req:20"),
            ],
            [
                InlineKeyboardButton(text="Заявки (последние 50)", callback_data="panel:req:50"),
            ],
            [
                InlineKeyboardButton(text="Водители на линии", callback_data="panel:drivers"),
            ],
        ]
    )


def build_drivers_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Обновить", callback_data="panel:drivers"),
                InlineKeyboardButton(text="+1", callback_data="panel:drivers_add:1"),
                InlineKeyboardButton(text="-1", callback_data="panel:drivers_add:-1"),
            ],
            [
                InlineKeyboardButton(text="Установить число…", callback_data="panel:drivers_set"),
            ],
            [
                InlineKeyboardButton(text="Назад", callback_data="panel:home"),
            ],
        ]
    )


def build_requests_list_text(items: list[dict]) -> str:
    blocks: list[str] = []
    for r in items:
        created = r["created_at"].strftime("%Y-%m-%d %H:%M")
        user_tag = _user_tag_from_row(r)
        maps = r.get("yandex_link") or _yandex_maps_link_from_geo(r.get("geo"))

        block_lines = [
            f"#{r['id']} | {created} | {r.get('status')}",
            f"Пользователь: {user_tag}",
            f"Телефон: {r.get('phone_formatted') or '—'}",
            f"Марка: {r.get('car_brand') or '—'}",
            f"Адрес: {r.get('address') or '—'}",
        ]
        if maps:
            block_lines.append(f"Яндекс.Карты: {maps}")

        block_lines.append(f"Подробно: /request {r['id']}")
        blocks.append("\n".join(block_lines))

    return "Последние заявки:\n\n" + ("\n\n".join(blocks) if blocks else "Заявок пока нет.")


def build_requests_list_kb(items: list[dict], limit: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Панель", callback_data="panel:home")]]
    for r in items:
        rows.append([InlineKeyboardButton(text=f"Подробнее #{r['id']}", callback_data=f"req:{r['id']}:{limit}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def format_request_details(r: dict) -> str:
    user_tag = _user_tag_from_row(r)
    maps = r.get("yandex_link") or _yandex_maps_link_from_geo(r.get("geo"))

    lines = [
        f"Заявка #{r['id']}",
        f"Статус: {r.get('status')}",
        f"Создана: {r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}",
        f"Пользователь: {user_tag}",
        f"Телефон: {r.get('phone_formatted') or r.get('phone') or '—'}",
        f"Марка: {r.get('car_brand') or '—'}",
        f"Адрес: {r.get('address') or '—'}",
        f"Гео: {r.get('geo') or '—'}",
    ]
    if maps:
        lines.append(f"Яндекс.Карты: {maps}")

    lines.append("")
    lines.append(f"Команда: /setstatus {r['id']} <new|in_work|done|cancel>")
    return "\n".join(lines)


def build_request_details_kb(req_id: int, limit: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Панель", callback_data="panel:home"),
                InlineKeyboardButton(text="Назад к списку", callback_data=f"back:{limit}"),
            ],
            [
                InlineKeyboardButton(text="Новая", callback_data=f"st:{req_id}:new:{limit}"),
                InlineKeyboardButton(text="В работу", callback_data=f"st:{req_id}:in_work:{limit}"),
            ],
            [
                InlineKeyboardButton(text="Готово", callback_data=f"st:{req_id}:done:{limit}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"st:{req_id}:cancel:{limit}"),
            ],
        ]
    )


async def safe_edit_message(cb: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=reply_markup)


# ---------------- /start ----------------

@dp.message(CommandStart())
async def start(message: Message) -> None:
    if is_dispatcher(message):
        try:
            drivers = await api_get_drivers()
        except Exception:
            drivers = None
        await message.answer(build_manager_panel_text(drivers), reply_markup=build_manager_panel_kb())
        return

    if not WEBAPP_URL or not API_BASE_URL:
        await message.answer("Сервис временно недоступен (не заданы WEBAPP_URL/API_BASE_URL).")
        return

    uid = message.from_user.id
    if uid not in greeted_users:
        greeted_users.add(uid)
        await message.answer(START_TEXT)

    try:
        drivers = await api_get_drivers()
    except Exception:
        drivers = 0

    api_url = API_BASE_URL.rstrip("/") + "/api/drivers"
    webapp_url = with_query(WEBAPP_URL, drivers=drivers, api=api_url)

    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Заказать эвакуатор", web_app=WebAppInfo(url=webapp_url))]],
        resize_keyboard=True,
    )
    await message.answer("Откройте мини‑апп и отправьте заявку.", reply_markup=kb)


# ---------------- Manager commands ----------------

@dp.message(Command("panel"))
async def panel_cmd(message: Message) -> None:
    if not is_dispatcher(message):
        return
    try:
        drivers = await api_get_drivers()
    except Exception:
        drivers = None
    await message.answer(build_manager_panel_text(drivers), reply_markup=build_manager_panel_kb())


@dp.message(Command("requests"))
async def requests_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return

    arg = (command.args or "").strip()
    limit = 10
    if arg:
        try:
            limit = int(arg)
        except Exception:
            limit = 10
    limit = max(1, min(50, limit))

    items = await db_list_requests(limit)
    await message.answer(build_requests_list_text(items), reply_markup=build_requests_list_kb(items, limit))


@dp.message(Command("request"))
async def request_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /request <id>")
        return
    try:
        req_id = int(arg)
    except Exception:
        await message.answer("ID должен быть числом.")
        return

    r = await db_get_request(req_id)
    if not r:
        await message.answer("Заявка не найдена.")
        return

    await message.answer(format_request_details(r))


@dp.message(Command("setstatus"))
async def setstatus_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return
    args = (command.args or "").strip().split()
    if len(args) != 2:
        await message.answer("Использование: /setstatus <id> <new|in_work|done|cancel>")
        return
    try:
        req_id = int(args[0])
    except Exception:
        await message.answer("ID должен быть числом.")
        return
    status = args[1].strip()
    if status not in ALLOWED_STATUSES:
        await message.answer("Статус должен быть: new, in_work, done, cancel")
        return

    ok = await db_set_status(req_id, status)
    if not ok:
        await message.answer("Заявка не найдена.")
        return
    await message.answer(f"Готово. Заявка #{req_id} теперь со статусом: {status}")


@dp.callback_query(F.data == "panel:home")
async def cb_panel_home(cb: CallbackQuery) -> None:
    if not is_dispatcher_user_id(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return

    try:
        drivers = await api_get_drivers()
    except Exception:
        drivers = None

    await cb.answer()
    await safe_edit_message(cb, build_manager_panel_text(drivers), reply_markup=build_manager_panel_kb())


@dp.callback_query(F.data.startswith("panel:req:"))
async def cb_panel_requests(cb: CallbackQuery) -> None:
    if not is_dispatcher_user_id(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return

    try:
        limit = int(cb.data.split(":")[2])
        limit = max(1, min(50, limit))
    except Exception:
        limit = 10

    items = await db_list_requests(limit)
    await cb.answer()
    await safe_edit_message(cb, build_requests_list_text(items), reply_markup=build_requests_list_kb(items, limit))


@dp.callback_query(F.data == "panel:drivers")
async def cb_panel_drivers(cb: CallbackQuery) -> None:
    if not is_dispatcher_user_id(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return

    try:
        drivers = await api_get_drivers()
        text = f"Водителей на линии: {drivers}"
    except Exception:
        text = "Не удалось получить число водителей из API."

    await cb.answer()
    await safe_edit_message(cb, text, reply_markup=build_drivers_kb())


@dp.callback_query(F.data.startswith("panel:drivers_add:"))
async def cb_panel_drivers_add(cb: CallbackQuery) -> None:
    if not is_dispatcher_user_id(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return

    try:
        delta = int(cb.data.split(":")[2])
    except Exception:
        await cb.answer("Ошибка", show_alert=True)
        return

    try:
        cur = await api_get_drivers()
        new_n = await api_set_drivers(max(0, cur + delta))
        text = f"Водителей на линии: {new_n}"
        await cb.answer("Готово")
    except Exception:
        text = "Ошибка при изменении. Проверьте API_BASE_URL и API_ADMIN_TOKEN."
        await cb.answer("Ошибка", show_alert=True)

    await safe_edit_message(cb, text, reply_markup=build_drivers_kb())


@dp.callback_query(F.data == "panel:drivers_set")
async def cb_panel_drivers_set(cb: CallbackQuery) -> None:
    if not is_dispatcher_user_id(cb.from_user.id):
        await cb.answer("Недоступно", show_alert=True)
        return

    MANAGER_AWAIT[cb.from_user.id] = "setdrivers"
    await cb.answer()
    await cb.message.answer("Отправьте число водителей одним сообщением (например: 7).")


@dp.message(F.text)
async def manager_number_input(message: Message) -> None:
    if not is_dispatcher(message):
        return

    mode = MANAGER_AWAIT.get(message.from_user.id)
    if mode != "setdrivers":
        return

    txt = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,6}", txt):
        await message.answer("Нужно отправить целое число (например: 5).")
        return

    n = int(txt)
    try:
        new_n = await api_set_drivers(n)
        MANAGER_AWAIT.pop(message.from_user.id, None)
        await message.answer(f"Готово. Водителей на линии теперь: {new_n}")
        await message.answer(build_manager_panel_text(new_n), reply_markup=build_manager_panel_kb())
    except Exception:
        await message.answer("Ошибка установки. Проверьте API_BASE_URL и API_ADMIN_TOKEN.")


# ---------------- WebApp orders ----------------

@dp.message(F.web_app_data)
async def webapp_data_handler(message: Message) -> None:
    uid = message.from_user.id
    now = time.time()

    last = last_request_ts.get(uid)
    if last is not None and (now - last) < COOLDOWN_SECONDS:
        remain = int(COOLDOWN_SECONDS - (now - last))
        await message.answer(
            "Заявку можно отправлять не чаще 1 раза в 5 минут.\n"
            f"Попробуйте через {remain//60:02d}:{remain%60:02d}."
        )
        return

    raw = message.web_app_data.data
    try:
        data = json.loads(raw)
    except Exception:
        data = {"raw": raw}

    sender = message.from_user
    req_id = await db_create_request(u=sender, payload=data)

    phone = _clean(data.get("phoneFormatted") or data.get("phone"))
    address = _clean(data.get("address"))
    car_brand = _clean(data.get("carBrand"))
    geo = _clean(data.get("geo"))
    ts = data.get("ts")
    yandex_link = _yandex_maps_link_from_geo(data.get("geo"))

    sender_line = (
        f"{sender.full_name}"
        + (f" (@{sender.username})" if sender.username else "")
    )

    lines = [
        f"Заявка на эвакуатор (ID: {req_id})",
        "",
        f"Время: {_dt(ts)}",
        f"Клиент: {sender_line}",
        f"Телефон: {phone}",
        f"Марка: {car_brand}",
        f"Адрес: {address}",
        f"Гео: {geo}",
        "Статус: new",
    ]
    if yandex_link:
        lines.append(f"Яндекс.Карты: {yandex_link}")

    await message.bot.send_message(TARGET_USER_ID, "\n".join(lines))

    last_request_ts[uid] = now
    await message.answer(
        "Заявка отправлена, ожидайте, с вами свяжется диспетчер, обычно до 5 минут. "
        "По истечению этого времени, наберите по номеру +7 (965) 747-07-27"
    )


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    if not TARGET_USER_ID:
        raise RuntimeError("TARGET_USER_ID не задан или 0")
    if not WEBAPP_URL:
        raise RuntimeError("WEBAPP_URL не задан")
    if not API_BASE_URL:
        raise RuntimeError("API_BASE_URL не задан")
    if not API_ADMIN_TOKEN:
        raise RuntimeError("API_ADMIN_TOKEN не задан (должен совпадать с API)")
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL не задан")

    await db_init()
    await http_init()

    bot = Bot(token=BOT_TOKEN)
    try:
        await dp.start_polling(bot)
    finally:
        await http_close()
        await db_close()


if __name__ == "__main__":
    asyncio.run(main())
