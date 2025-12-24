import os
import hmac
import json
import time
import hashlib
from typing import Any
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
TARGET_USER_ID = int(os.getenv("TARGET_USER_ID", "0"))
API_ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN")  # optional fallback

POOL: asyncpg.Pool | None = None


class DriversPayload(BaseModel):
    drivers_on_line: int


class StatusPayload(BaseModel):
    status: str


def _parse_qs(qs: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in (qs or "").split("&"):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k] = v
    return out


def _tg_webapp_check_init_data(init_data: str, bot_token: str) -> dict[str, Any]:
    from urllib.parse import unquote

    data = _parse_qs(init_data)
    their_hash = data.pop("hash", None)
    if not their_hash:
        raise HTTPException(401, "No hash in initData")

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, their_hash):
        raise HTTPException(401, "Bad initData hash")

    auth_date = int(data.get("auth_date", "0") or "0")
    if not auth_date:
        raise HTTPException(401, "No auth_date")
    if time.time() - auth_date > 86400:
        raise HTTPException(401, "initData expired")

    user_raw = data.get("user")
    if not user_raw:
        raise HTTPException(401, "No user in initData")

    try:
        user = json.loads(unquote(user_raw))
    except Exception:
        raise HTTPException(401, "Bad user json in initData")

    return user


def _require_admin(x_tg_init_data: str | None, x_admin_token: str | None) -> dict[str, Any]:
    if x_tg_init_data:
        if not BOT_TOKEN:
            raise HTTPException(500, "BOT_TOKEN is required for initData auth")
        user = _tg_webapp_check_init_data(x_tg_init_data, BOT_TOKEN)
        uid = int(user.get("id", 0) or 0)
        if not TARGET_USER_ID:
            raise HTTPException(500, "TARGET_USER_ID not set")
        if uid != TARGET_USER_ID:
            raise HTTPException(403, "Not an admin")
        return user

    if API_ADMIN_TOKEN and x_admin_token == API_ADMIN_TOKEN:
        if not TARGET_USER_ID:
            raise HTTPException(500, "TARGET_USER_ID not set")
        return {"id": TARGET_USER_ID, "token": "fallback"}

    raise HTTPException(401, "Unauthorized")


async def _get_setting(key: str, default: Any) -> Any:
    async with POOL.acquire() as con:
        row = await con.fetchrow("SELECT value_json FROM settings WHERE key=$1", key)
        if not row:
            return default
        v = row["value_json"]
        return v if v is not None else default


async def _set_setting(key: str, value: Any) -> Any:
    async with POOL.acquire() as con:
        await con.execute(
            """
            INSERT INTO settings(key, value_json)
            VALUES($1, $2::jsonb)
            ON CONFLICT(key) DO UPDATE SET value_json=EXCLUDED.value_json
            """,
            key,
            json.dumps(value, ensure_ascii=False),
        )
    return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    global POOL
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set")
    POOL = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with POOL.acquire() as con:
        await con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value_json JSONB NOT NULL
        );
        """)
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
        row = await con.fetchrow("SELECT 1 FROM settings WHERE key='drivers_on_line'")
        if not row:
            await con.execute(
                "INSERT INTO settings(key, value_json) VALUES('drivers_on_line', $1::jsonb)",
                json.dumps(0),
            )

    yield

    if POOL:
        await POOL.close()
        POOL = None


app = FastAPI(title="Tow API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Render/healthcheck helpers
@app.get("/")
async def root():
    return {"ok": True}

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/drivers")
async def get_drivers():
    v = await _get_setting("drivers_on_line", 0)
    try:
        n = int(v)
    except Exception:
        n = 0
    return {"drivers_on_line": n}


@app.get("/api/admin/me")
async def admin_me(
    x_tg_init_data: str | None = Header(default=None, alias="X-Tg-Init-Data"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    user = _require_admin(x_tg_init_data, x_admin_token)
    return {"ok": True, "user": {"id": user.get("id"), "username": user.get("username")}}


@app.post("/api/admin/drivers")
async def set_drivers(
    payload: DriversPayload,
    x_tg_init_data: str | None = Header(default=None, alias="X-Tg-Init-Data"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_tg_init_data, x_admin_token)
    n = int(payload.drivers_on_line)
    if n < 0:
        n = 0
    await _set_setting("drivers_on_line", n)
    return {"drivers_on_line": n}


@app.get("/api/admin/requests")
async def admin_list_requests(
    limit: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    x_tg_init_data: str | None = Header(default=None, alias="X-Tg-Init-Data"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_tg_init_data, x_admin_token)

    where = ""
    args: list[Any] = [limit]
    if status:
        where = "WHERE status = $2"
        args.append(status)

    async with POOL.acquire() as con:
        rows = await con.fetch(
            f"""
            SELECT id, created_at, status,
                   tg_user_id, tg_username, tg_full_name,
                   phone_formatted, phone, car_brand, address, geo, yandex_link
            FROM requests
            {where}
            ORDER BY created_at DESC
            LIMIT $1
            """,
            *args
        )
    return {"items": [dict(r) for r in rows]}


@app.get("/api/admin/requests/{req_id}")
async def admin_get_request(
    req_id: int,
    x_tg_init_data: str | None = Header(default=None, alias="X-Tg-Init-Data"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_tg_init_data, x_admin_token)
    async with POOL.acquire() as con:
        row = await con.fetchrow("SELECT * FROM requests WHERE id=$1", req_id)
    if not row:
        raise HTTPException(404, "Not found")
    return {"item": dict(row)}


@app.post("/api/admin/requests/{req_id}/status")
async def admin_set_request_status(
    req_id: int,
    payload: StatusPayload,
    x_tg_init_data: str | None = Header(default=None, alias="X-Tg-Init-Data"),
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_tg_init_data, x_admin_token)
    status = (payload.status or "").strip()
    if status not in {"new", "in_work", "done", "cancel"}:
        raise HTTPException(400, "Bad status")

    async with POOL.acquire() as con:
        res = await con.execute("UPDATE requests SET status=$2 WHERE id=$1", req_id, status)
        if res.split()[-1] == "0":
            raise HTTPException(404, "Not found")
        row = await con.fetchrow("SELECT id, created_at, status FROM requests WHERE id=$1", req_id)

    return {"item": dict(row)}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "10000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
