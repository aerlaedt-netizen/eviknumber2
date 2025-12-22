import os
import json
import time
import asyncio
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import asyncpg
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo

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


def is_dispatcher(message: Message) -> bool:
    return bool(message.from_user) and message.from_user.id == TARGET_USER_ID


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


# ---------------- DB (только заявки/статусы) ----------------

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
            "SELECT id, created_at, phone_formatted, car_brand, address, status "
            "FROM requests ORDER BY created_at DESC LIMIT $1",
            limit
        )
        return [dict(r) for r in rows]


async def db_set_status(req_id: int, status: str) -> bool:
    async with DB_POOL.acquire() as con:
        res = await con.execute("UPDATE requests SET status=$2 WHERE id=$1", req_id, status)
        return res.split()[-1] != "0"


# ---------------- HTTP (к вашему API сервису) ----------------

async def http_init():
    global HTTP
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))


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
    url = API_BASE_URL.rstrip("/") + "/api/admin/drivers"
    headers = {"X-Admin-Token": API_ADMIN_TOKEN or ""}
    async with HTTP.post(url, json={"drivers_on_line": int(n)}, headers=headers) as r:
        j = await r.json()
        if r.status != 200:
            raise RuntimeError(f"API error {r.status}: {j}")
        return int(j["drivers_on_line"])


# ---------------- Public ----------------

@dp.message(Command("start"))
async def start(message: Message) -> None:
    if not WEBAPP_URL or not API_BASE_URL:
        await message.answer("Сервис временно недоступен (не заданы WEBAPP_URL/API_BASE_URL).")
        return

    uid = message.from_user.id
    if uid not in greeted_users:
        greeted_users.add(uid)
        await message.answer(START_TEXT)

    # Быстро подставим initial drivers из API (для мгновенного отображения)
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


# ---------------- Dispatcher commands (hidden for others) ----------------

@dp.message(Command("help"))
async def help_cmd(message: Message) -> None:
    if not is_dispatcher(message):
        return
    await message.answer(
        "Команды диспетчера:\n"
        "/help — показать команды\n\n"
        "Водители (в API, не в БД):\n"
        "/drivers — текущее количество\n"
        "/setdrivers <n> — установить\n"
        "/adddrivers <n> — прибавить\n"
        "/deldrivers <n> — убавить\n\n"
        "Заявки (Postgres):\n"
        "/requests [n] — последние n заявок (по умолчанию 10, максимум 50)\n"
        "/request <id> — подробности по заявке\n"
        "/setstatus <id> <new|in_work|done|cancel> — сменить статус\n"
    )


@dp.message(Command("drivers"))
async def drivers_cmd(message: Message) -> None:
    if not is_dispatcher(message):
        return
    try:
        drivers = await api_get_drivers()
        await message.answer(f"Водителей на линии сейчас: {drivers}")
    except Exception:
        await message.answer("Не удалось получить число водителей из API.")


@dp.message(Command("setdrivers"))
async def setdrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /setdrivers <число>")
        return
    try:
        n = int(arg)
        if n < 0:
            n = 0
        new_n = await api_set_drivers(n)
        await message.answer(f"Готово. Водителей на линии теперь: {new_n}")
    except Exception:
        await message.answer("Ошибка установки. Проверьте API_BASE_URL и API_ADMIN_TOKEN.")


@dp.message(Command("adddrivers"))
async def adddrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /adddrivers <число>")
        return
    try:
        delta = int(arg)
        if delta < 0:
            await message.answer("Нужно число ≥ 0.")
            return
        cur = await api_get_drivers()
        new_n = await api_set_drivers(cur + delta)
        await message.answer(f"Готово. Водителей на линии теперь: {new_n}")
    except Exception:
        await message.answer("Ошибка. Проверьте API_BASE_URL и API_ADMIN_TOKEN.")


@dp.message(Command("deldrivers"))
async def deldrivers_cmd(message: Message, command: CommandObject) -> None:
    if not is_dispatcher(message):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Использование: /deldrivers <число>")
        return
    try:
        delta = int(arg)
        if delta < 0:
            await message.answer("Нужно число ≥ 0.")
            return
        cur = await api_get_drivers()
        new_n = await api_set_drivers(max(0, cur - delta))
        await message.answer(f"Готово. Водителей на линии теперь: {new_n}")
    except Exception:
        await message.answer("Ошибка. Проверьте API_BASE_URL и API_ADMIN_TOKEN.")


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

    items = await db_list_requests(limit)
    if not items:
        await message.answer("Заявок пока нет.")
        return

    lines = ["Последние заявки:"]
    for r in items:
        created = r["created_at"].strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"#{r['id']} | {created} | {r.get('status')} | {r.get('phone_formatted') or '—'} | "
            f"{(r.get('car_brand') or '—')} | {(r.get('address') or '—')}"
        )
    await message.answer("\n".join(lines))


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

    lines = [
        f"Заявка #{r['id']} ({r['status']})",
        f"Создана: {r['created_at'].strftime('%Y-%m-%d %H:%M:%S')}",
        f"Клиент: {r.get('tg_full_name') or '—'} (id={r.get('tg_user_id') or '—'}"
        + (f", @{r['tg_username']}" if r.get("tg_username") else "")
        + ")",
        f"Телефон: {r.get('phone_formatted') or r.get('phone') or '—'}",
        f"Марка: {r.get('car_brand') or '—'}",
        f"Адрес: {r.get('address') or '—'}",
        f"Гео: {r.get('geo') or '—'}",
    ]
    if r.get("yandex_link"):
        lines.append(f"Яндекс.Карты: {r['yandex_link']}")
    await message.answer("\n".join(lines))


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


# ---------------- WebApp заявки ----------------

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
        f"{sender.full_name} (id={sender.id}"
        + (f", @{sender.username}" if sender.username else "")
        + ")"
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
    await message.answer("Заявка отправлена, ожидайте, с вами свяжется диспетчер, обычно до 10 минут")


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
