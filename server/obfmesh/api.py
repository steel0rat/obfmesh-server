"""REST and SSE endpoints of obfmesh, exactly the table from SPEC.md.

    GET    /api/status                admin   version, spoke count, per-spoke state, counters
    GET    /api/settings              admin   {spokes, masking, mtu, external_host, port_base, key}
    PATCH  /api/settings              admin   apply changes, bump config_version, reconcile(), emit SSE
    GET    /api/clients               admin   client list, secrets shown as "<есть>"
    POST   /api/clients               admin   {name} -> client plus a one-time token
    DELETE /api/clients/{name}        admin   remove client
    GET    /api/clients/{name}/bundle admin   bundle of any client
    GET    /api/bundle                client  own bundle (Bearer)
    GET    /api/events                both    SSE stream of config_version changes
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, config_gen, db, orchestrator
from .auth import (
    SECRET_PLACEHOLDER,
    Principal,
    redact,
    register_secret,
    require_admin,
    require_admin_or_client,
    require_client,
)
from .models import (
    CLIENT_NAME_RE,
    MTU_MAX,
    MTU_MIN,
    OBFUSCATION_KEY_RE,
    SPOKE_MAX,
    SPOKE_MIN,
    Masking,
)

LOG = logging.getLogger("obfmesh.api")

router = APIRouter(prefix="/api")

CLIENT_NAME_PATTERN = CLIENT_NAME_RE.pattern

# Responses that carry key material must not be stored anywhere on the way.
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # ask buffering proxies to pass chunks through
}
SSE_KEEPALIVE_SECONDS = 20.0
SSE_RETRY_MS = 3000
# Database.wait_for_change() blocks a worker thread, so the wait stays short: a missed
# condition notification is picked up on the next turn and shutdown is never delayed.
VERSION_WATCH_TIMEOUT = 2.0

# Field names whose values never leave the process in clear text (SPEC invariant 1).
# The bundle endpoints bypass this deliberately: key material is their payload.
SECRET_FIELDS = frozenset(
    {
        "key",
        "obfuscation_key",
        "token",
        "token_hash",
        "secret",
        "admin_key",
        "private_key",
        "privkey",
        "wg_private_key",
        "psk",
        "preshared_key",
        "wg_preshared_key",
    }
)

# Serialises read-modify-write plus reconcile(): concurrent PATCH/POST/DELETE would otherwise
# race both on the stored state and on the ip/wg calls. Single uvicorn worker only.
_APPLY_LOCK = asyncio.Lock()


# --------------------------------------------------------------------------- request bodies


class SettingsPatch(BaseModel):
    """PATCH /api/settings body: every field optional, unknown fields rejected.

    Bounds repeat models.Settings.validate() so that a bad value comes back as 422 with a
    field name instead of a validation error raised deep in the core.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=True)

    # Aggregation modes are gone: agg_mode here would be rejected as an extra
    # field, which is the honest answer - the server cannot honour it any more.
    spokes: int | None = Field(default=None, ge=SPOKE_MIN, le=SPOKE_MAX)
    masking: Masking | None = None
    mtu: int | None = Field(default=None, ge=MTU_MIN, le=MTU_MAX)
    external_host: str | None = Field(default=None, min_length=1, max_length=253)
    # SPEC invariant 6 leaves room for 10 spokes above the base.
    port_base: int | None = Field(default=None, ge=1024, le=65535 - SPOKE_MAX)
    key: str | None = Field(default=None, pattern=OBFUSCATION_KEY_RE.pattern)
    obfuscator_bin: str | None = Field(default=None, min_length=1, pattern=r"^/\S*$")


class ClientCreate(BaseModel):
    """POST /api/clients body."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=CLIENT_NAME_PATTERN)


# --------------------------------------------------------------------------- SSE plumbing


class EventBroker:
    """Fan-out of config_version changes to the connected SSE readers.

    Per-subscriber depth is 1: a reader that fell behind only needs the newest version,
    so a queued event is replaced instead of piling up.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, payload: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                LOG.warning("dropped an SSE event for a stalled subscriber")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


BROKER = EventBroker()


def _sse_frame(event: str, data: dict[str, Any], retry_ms: int | None = None) -> str:
    lines = [f"event: {event}"]
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    lines.append("data: " + json.dumps(data, separators=(",", ":"), ensure_ascii=False))
    return "\n".join(lines) + "\n\n"


async def watch_config_version() -> None:
    """Single publisher for the SSE stream, started by the application lifespan.

    Reads Database.wait_for_change() in a worker thread, so a version bump made anywhere in
    this process (settings update, orchestrator) reaches every subscriber. One thread total,
    regardless of how many readers are connected.
    """
    database = db.get_db()
    version = await run_in_threadpool(database.get_config_version)
    while True:
        try:
            changed = await run_in_threadpool(
                database.wait_for_change, version, VERSION_WATCH_TIMEOUT
            )
            if changed is None or changed == version:
                continue
            version = changed
            # Wait out an apply that is still running: subscribers should only be told about a
            # config_version whose reconcile() has already finished.
            async with _APPLY_LOCK:
                pass
            LOG.info(
                "config_version=%d, notifying %d subscriber(s)", version, BROKER.subscriber_count
            )
            BROKER.publish({"config_version": version})
        except Exception:
            # A database hiccup must not kill the only publisher of the stream.
            LOG.exception("config_version watcher failed, retrying")
            await asyncio.sleep(VERSION_WATCH_TIMEOUT)


async def _event_stream(request: Request, principal: Principal) -> AsyncIterator[str]:
    queue = BROKER.subscribe()
    peer = request.client.host if request.client else "unknown"
    LOG.info("SSE open: %s (%s), subscribers=%d", principal.name, peer, BROKER.subscriber_count)
    try:
        yield _sse_frame(
            "hello",
            {"config_version": db.get_db().get_config_version()},
            retry_ms=SSE_RETRY_MS,
        )
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_SECONDS)
            except (asyncio.TimeoutError, TimeoutError):
                # A comment frame keeps proxies from timing the connection out and turns a dead
                # peer into a write error, which cancels this generator.
                yield ": keepalive\n\n"
                continue
            yield _sse_frame("config", payload)
    finally:
        # Runs on client disconnect, on cancellation and on shutdown alike: nothing is left behind.
        BROKER.unsubscribe(queue)
        LOG.info("SSE close: %s (%s), subscribers=%d", principal.name, peer, BROKER.subscriber_count)


# --------------------------------------------------------------------------- helpers


def _scrub(value: Any) -> Any:
    """Replace secret-looking values with the SPEC placeholder, recursively."""
    if isinstance(value, dict):
        return {
            k: SECRET_PLACEHOLDER
            if (k.lower() in SECRET_FIELDS and v not in (None, "", [], {}))
            else _scrub(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


async def run_reconcile() -> Any:
    """Bring the running system to the stored state, returning the orchestrator report.

    orchestrator.reconcile() shells out to ip/wg and manages the obfuscator processes, so it runs
    in a worker thread instead of on the event loop.
    """
    if inspect.iscoroutinefunction(orchestrator.reconcile):
        return await orchestrator.reconcile()
    return await run_in_threadpool(orchestrator.reconcile)


def reconcile_summary(report: Any) -> dict[str, Any]:
    """Compact report for an API response: what changed, what broke, which version came out.

    The list of executed commands is left out - it is long, and a rendered obfuscator command
    line is exactly the kind of string that must not travel in an HTTP body.
    """
    data = report.to_dict() if hasattr(report, "to_dict") else dict(report or {})
    return {
        "ok": bool(data.get("ok", True)),
        "changes": [redact(str(item)) for item in data.get("changes", [])],
        "errors": [redact(str(item)) for item in data.get("errors", [])],
        # masking=NONE и подобное: настройка принята, но так работать нельзя.
        "warnings": [redact(str(item)) for item in data.get("warnings", [])],
        "config_version": data.get("config_version") or None,
        "duration_ms": data.get("duration_ms"),
        "dry_run": bool(data.get("dry_run", False)),
    }


async def _apply(action: str) -> dict[str, Any]:
    """reconcile() after a state change. A partial failure is reported, never swallowed."""
    try:
        report = await run_reconcile()
    except Exception:
        # The message can carry a command line, so it only goes to the log, where the redaction
        # filter from auth.py strips the registered secrets.
        LOG.exception("reconcile() failed after %s", action)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{action}: state was stored but reconcile() failed, see the server log",
        ) from None

    summary = reconcile_summary(report)
    if not summary["ok"]:
        LOG.error("reconcile after %s finished with errors: %s", action, summary["errors"])
    for warning in summary["warnings"]:
        LOG.warning("reconcile after %s: %s", action, warning)
    return summary


def _bundle_response(bundle: dict[str, Any], request: Request) -> Response:
    """Bundle plus a strong ETag, so a polling client can skip an unchanged payload."""
    etag = '"' + config_gen.bundle_etag(bundle) + '"'
    if request.headers.get("if-none-match", "").strip() == etag:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag, **NO_STORE})
    return JSONResponse(bundle, headers={"ETag": etag, **NO_STORE})


# --------------------------------------------------------------------------- endpoints


@router.get("/status")
async def get_status(_: Principal = Depends(require_admin)) -> dict[str, Any]:
    """Version, spoke count, per-spoke state and counters.

    The shape comes from orchestrator.status(); this layer only adds what it alone knows and
    runs the whole payload through the secret scrubber once more.
    """
    payload = _scrub(jsonable_encoder(await run_in_threadpool(orchestrator.status)))
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="orchestrator.status() must return a mapping",
        )
    payload.setdefault("version", __version__)
    payload["sse_subscribers"] = BROKER.subscriber_count
    return payload


@router.get("/settings")
async def get_settings(_: Principal = Depends(require_admin)) -> Response:
    return JSONResponse(db.get_db().get_settings().to_api(), headers=NO_STORE)


@router.patch("/settings")
async def patch_settings(payload: SettingsPatch, _: Principal = Depends(require_admin)) -> Response:
    """Apply the changed fields, bump config_version, reconcile; the watcher emits the SSE event."""
    submitted = payload.model_dump(mode="json", exclude_unset=True)
    nulls = sorted(name for name, value in submitted.items() if value is None)
    if nulls:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"fields cannot be set to null: {', '.join(nulls)}",
        )

    database = db.get_db()
    async with _APPLY_LOCK:
        if "key" in submitted:
            register_secret(submitted["key"])
        settings, changed = await run_in_threadpool(database.update_settings, submitted)
        if not changed:
            # Idempotent: nothing moved, so no reconcile and no event.
            return JSONResponse(
                dict(settings.to_api(), config_version=settings.config_version), headers=NO_STORE
            )
        LOG.info(
            "settings changed: %s (config_version=%d)",
            ", ".join(sorted(submitted)),
            settings.config_version,
        )
        # reconcile() can raise config_version once more (new spokes change every bundle),
        # so the answer carries the version the report came back with.
        summary = await _apply("settings update")

    return JSONResponse(
        dict(
            settings.to_api(),
            config_version=summary["config_version"] or settings.config_version,
            reconcile=summary,
        ),
        headers=NO_STORE,
    )


@router.get("/clients")
async def list_clients(_: Principal = Depends(require_admin)) -> list[dict[str, Any]]:
    return [client.safe_dict() for client in db.get_db().list_clients()]


@router.post("/clients", status_code=status.HTTP_201_CREATED)
async def create_client(body: ClientCreate, _: Principal = Depends(require_admin)) -> Response:
    """Create a client with key material for every spoke; the token is shown this one time."""
    database = db.get_db()
    async with _APPLY_LOCK:
        if database.get_client(body.name) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"клиент {body.name!r} уже есть"
            )
        client, token = await run_in_threadpool(database.create_client, body.name)
        LOG.info("client created: %s", body.name)
        # reconcile() is what puts the fresh peer on every swg{i}.
        summary = await _apply("client creation")

    return JSONResponse(
        {
            "name": client.name,
            "token": token,
            "client": client.safe_dict(),
            "reconcile": summary,
        },
        status_code=status.HTTP_201_CREATED,
        headers=NO_STORE,
    )


@router.delete("/clients/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    name: str = Path(pattern=CLIENT_NAME_PATTERN),
    _: Principal = Depends(require_admin),
) -> Response:
    database = db.get_db()
    async with _APPLY_LOCK:
        if not await run_in_threadpool(database.delete_client, name):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"нет клиента {name!r}")
        LOG.info("client deleted: %s", name)
        # Peers are removed from the live interfaces here.
        await _apply("client removal")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/clients/{name}/bundle")
async def get_client_bundle(
    request: Request,
    name: str = Path(pattern=CLIENT_NAME_PATTERN),
    _: Principal = Depends(require_admin),
) -> Response:
    database = db.get_db()
    client = database.get_client(name)
    if client is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"нет клиента {name!r}")
    bundle = await run_in_threadpool(config_gen.bundle_dict, client)
    return _bundle_response(bundle, request)


@router.get("/bundle")
async def get_own_bundle(
    request: Request, principal: Principal = Depends(require_client)
) -> Response:
    bundle = await run_in_threadpool(config_gen.bundle_dict, principal.client)
    return _bundle_response(bundle, request)


@router.get("/events")
async def events(
    request: Request, principal: Principal = Depends(require_admin_or_client)
) -> StreamingResponse:
    """SSE: `hello` on connect, `config` on every config_version change, `: keepalive` between."""
    return StreamingResponse(
        _event_stream(request, principal),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
