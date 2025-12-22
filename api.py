import os
import time
from aiohttp import web

PORT = int(os.getenv("PORT", "10000"))
API_ADMIN_TOKEN = os.getenv("API_ADMIN_TOKEN")

# drivers_on_line НЕ в БД: живёт в памяти API (не переживёт рестарт/деплой)
drivers_on_line: int = 0
drivers_updated_at: int = int(time.time())


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        resp = await handler(request)

    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Token"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def is_admin(request: web.Request) -> bool:
    token = request.headers.get("X-Admin-Token", "")
    return bool(API_ADMIN_TOKEN) and token == API_ADMIN_TOKEN


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def api_drivers(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "drivers_on_line": drivers_on_line,
            "updated_at": drivers_updated_at,
        }
    )


async def admin_set_drivers(request: web.Request) -> web.Response:
    if not is_admin(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        payload = {}

    try:
        n = int(payload.get("drivers_on_line"))
        if n < 0:
            n = 0
    except Exception:
        return web.json_response({"ok": False, "error": "bad drivers_on_line"}, status=400)

    global drivers_on_line, drivers_updated_at
    drivers_on_line = n
    drivers_updated_at = int(time.time())

    return web.json_response({"ok": True, "drivers_on_line": drivers_on_line, "updated_at": drivers_updated_at})


def make_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/api/drivers", api_drivers)
    app.router.add_post("/api/admin/drivers", admin_set_drivers)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)
