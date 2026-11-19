"""Application assembly: routers, security middleware, error pages."""
from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import config
from .api import routes as api_routes
from .db import engine
from .middleware import BodyLimitMiddleware, BodyTooLarge, SecurityHeadersMiddleware
from .sync import client as sync_client
from .sync import routes as sync_routes
from .web import admin, ai_panel, auth, fleet, orders, views
from .web.deps import LoginRequired, render, wants_json

BASE = Path(__file__).resolve().parent.parent
log = logging.getLogger("sto")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    if config.IS_POSTGRES:
        from .rls import assert_runtime_role_is_restricted
        assert_runtime_role_is_restricted(engine)
    elif not config.IS_TEST:
        from .manage import init
        init()                                   # SQLite dev: schema + plans (+ demo data)
    sync_client.start()
    yield


def _is_api(request: Request) -> bool:
    return request.url.path.startswith(("/api/", "/sync")) or wants_json(request)


_HUGE_ID = re.compile(r"/-?\d{10,}(/|$)")    # beyond int4 on Postgres


def _is_out_of_range(exc: BaseException) -> bool:
    from sqlalchemy.exc import DataError
    orig = getattr(exc, "orig", None)
    if isinstance(exc, OverflowError) or isinstance(orig, OverflowError):
        return True                                  # SQLite: Python int too large to bind
    # Postgres: numeric_value_out_of_range (22003) only — other DataErrors are real bugs (500)
    return isinstance(exc, DataError) and getattr(orig, "sqlstate", None) == "22003"


def create_app() -> FastAPI:
    docs = config.ENABLE_API_DOCS
    app = FastAPI(title=config.APP_TITLE, lifespan=lifespan, docs_url="/docs" if docs else None,
                  redoc_url=None, openapi_url="/openapi.json" if docs else None)
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
    for r in (auth.router, ai_panel.router, orders.router, views.router, fleet.router,
              admin.router, api_routes.router, sync_routes.router):
        app.include_router(r)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.exception_handler(LoginRequired)
    async def _login(request: Request, _exc: LoginRequired) -> Any:
        if _is_api(request):
            return JSONResponse({"detail": "authentication required"}, status_code=401)
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt)}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> Any:
        detail = exc.detail if isinstance(exc.detail, str) else ""
        if _is_api(request):
            return JSONResponse({"detail": detail or "error"}, status_code=exc.status_code,
                                headers=getattr(exc, "headers", None))
        ctx = getattr(request.state, "ctx", None)
        return render(request, None, ctx, "error.html", status_code=exc.status_code,
                      code=exc.status_code, detail=detail if detail.startswith("err.") else "")

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError) -> Any:
        if _is_api(request):
            return JSONResponse({"detail": "invalid request"}, status_code=422)
        return render(request, None, getattr(request.state, "ctx", None), "error.html",
                      status_code=400, code=400, detail="err.bad_request")

    from sqlalchemy.exc import DataError, StatementError

    @app.exception_handler(OverflowError)
    @app.exception_handler(DataError)
    @app.exception_handler(StatementError)
    async def _out_of_range(request: Request, exc: Exception) -> Any:
        """An id that cannot exist is not found when it is in the path (/orders/99999999999),
        and a bad request when it came in a form field or the query string."""
        if _is_out_of_range(exc):
            log.warning("out-of-range value on %s %s: %s", request.method, request.url.path, type(exc).__name__)
            if _HUGE_ID.search(request.url.path):
                return await _http(request, StarletteHTTPException(404))
            if _is_api(request):
                return JSONResponse({"detail": "invalid request"}, status_code=400)
            return render(request, None, getattr(request.state, "ctx", None), "error.html",
                          status_code=400, code=400, detail="err.bad_request")
        return await _crash(request, exc)

    @app.exception_handler(BodyTooLarge)
    async def _too_large(_request: Request, _exc: BodyTooLarge) -> Any:
        return PlainTextResponse("request body too large", status_code=413)

    @app.exception_handler(Exception)
    async def _crash(request: Request, exc: Exception) -> Any:
        log.exception("unhandled error on %s", request.url.path)
        if _is_api(request):
            return JSONResponse({"detail": "internal error"}, status_code=500)
        return render(request, None, None, "error.html", status_code=500, code=500, detail="")

    return app


app = create_app()
