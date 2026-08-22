"""FastAPI application: wiring, lifespan, error mapping, static files.

Listens on loopback only (see obfmesh-server.service); TLS and the public route belong to Caddy,
so no CORS layer is installed - the UI is served from this same origin out of obfmesh/static.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, api, auth, db, orchestrator
from .config_gen import BundleError
from .db import Conflict, NotFound
from .models import ObfmeshError
from .models import ValidationError as ObfmeshValidationError

LOG = logging.getLogger("obfmesh.app")

ENV_STATIC_DIR = "OBFMESH_STATIC_DIR"
ENV_ROOT_PATH = "OBFMESH_ROOT_PATH"
ENV_LOG_LEVEL = "OBFMESH_LOG_LEVEL"
ENV_DOCS = "OBFMESH_DOCS"

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"


def static_dir() -> Path:
    override = os.environ.get(ENV_STATIC_DIR)
    return Path(override) if override else DEFAULT_STATIC_DIR


def _setup_logging() -> None:
    level = os.environ.get(ENV_LOG_LEVEL, "INFO").upper()
    root = logging.getLogger()
    if not root.handlers:
        # Under uvicorn only the uvicorn.* loggers get handlers; without this obfmesh records
        # would never reach the journal.
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    else:
        root.setLevel(level)
    auth.install_log_redaction()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Everything this process creates (SQLite file, key material) stays owner-only.
    os.umask(0o077)
    _setup_logging()

    admin_key = auth.ADMIN_KEYS.current()
    auth.register_secret(admin_key)
    if admin_key is None:
        LOG.error(
            "admin key is not configured (%s or %s): admin endpoints will answer 503",
            auth.ADMIN_KEYS.path,
            auth.ENV_ADMIN_KEY,
        )

    database = db.get_db()
    await run_in_threadpool(database.init)
    auth.register_secret(database.get_settings().obfuscation_key)

    orch = orchestrator.get_orchestrator()
    try:
        # Boot-time convergence: after a reboot or a service restart the interfaces and the
        # obfuscator processes have to be rebuilt from the stored state (surviving obfuscators
        # are picked up again by their pid files).
        summary = api.reconcile_summary(await api.run_reconcile())
        if not summary["ok"]:
            LOG.error("initial reconcile() reported errors: %s", summary["errors"])
    except Exception:
        LOG.exception("initial reconcile() failed; API stays up so the state can be fixed over HTTP")

    # Background thread of the orchestrator: restarts dead obfuscators and re-runs reconcile
    # periodically, so an external firewall flush does not stay unnoticed.
    orch.start_supervisor()

    watcher = asyncio.create_task(api.watch_config_version(), name="obfmesh-config-watch")
    LOG.info("obfmesh %s ready, db %s, static %s", __version__, database.path, static_dir())
    try:
        yield
    finally:
        watcher.cancel()
        # The watcher may sit in a worker thread; the wait is bounded by VERSION_WATCH_TIMEOUT.
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        # The obfuscators keep running on purpose: restarting the control plane must not drop
        # traffic, they are adopted again on the next start. Set OBFMESH_STOP_PROCESSES=1 (or
        # run `obfmesh-ctl teardown`) when a stop really has to take the spokes down.
        await run_in_threadpool(orch.shutdown)


def _json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status_code)


async def _on_validation_error(request: Request, exc: Exception) -> JSONResponse:
    return _json_error(422, str(exc))


async def _on_not_found(request: Request, exc: Exception) -> JSONResponse:
    return _json_error(404, str(exc))


async def _on_conflict(request: Request, exc: Exception) -> JSONResponse:
    return _json_error(409, str(exc))


async def _on_bundle_error(request: Request, exc: Exception) -> JSONResponse:
    # The state is not ready for a bundle yet; the message names what is missing.
    return _json_error(409, str(exc))


async def _on_obfmesh_error(request: Request, exc: Exception) -> JSONResponse:
    # Covers wg.CommandError and friends: their text carries a command line, so it only
    # goes to the log, which the redaction filter cleans.
    LOG.exception("core error on %s %s", request.method, request.url.path)
    return _json_error(500, "внутренняя ошибка, подробности в журнале сервера")


async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
    LOG.exception("unhandled error on %s %s", request.method, request.url.path)
    return _json_error(500, "internal server error")


def create_app() -> FastAPI:
    docs_enabled = os.environ.get(ENV_DOCS, "0") == "1"
    app = FastAPI(
        title="obfmesh",
        version=__version__,
        summary="Scalable obfuscated WireGuard control plane",
        root_path=os.environ.get(ENV_ROOT_PATH, ""),
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )
    app.include_router(api.router)

    # More specific classes first is not required - Starlette walks the exception MRO - but the
    # base ObfmeshError handler has to exist so a core failure never leaks a command line.
    app.add_exception_handler(ObfmeshValidationError, _on_validation_error)
    app.add_exception_handler(NotFound, _on_not_found)
    app.add_exception_handler(Conflict, _on_conflict)
    app.add_exception_handler(BundleError, _on_bundle_error)
    app.add_exception_handler(ObfmeshError, _on_obfmesh_error)
    app.add_exception_handler(Exception, _on_unhandled)

    # Mounted last so every /api/* route wins. A missing directory is left unmounted on
    # purpose: StaticFiles would answer 500 with a traceback on every request instead of 404.
    directory = static_dir()
    if directory.is_dir():
        app.mount("/", StaticFiles(directory=str(directory), html=True), name="static")
    else:
        LOG.warning("static directory %s is missing: only /api/* will answer", directory)
    return app


app = create_app()
