"""Authentication and secret hygiene for the obfmesh HTTP layer.

Two principals, per SPEC.md:
  * admin  — header ``X-API-Key``, value kept in a root-only file (/etc/obfmesh/admin.key);
  * client — header ``Authorization: Bearer <token>``, token issued by ``POST /api/clients``.

Neither the admin key nor client tokens are ever written to logs or error bodies. The log
redaction filter here is the second line of defence for SPEC invariant 1: the obfuscation key
lives in a rendered `key = ...` config file and WireGuard private keys are passed to `wg set`,
so either can surface inside a subprocess error raised somewhere in the core modules.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import HTTPException, Request, status

from . import db
from .models import MASKED

if TYPE_CHECKING:  # pragma: no cover - annotations only
    from .models import Client

LOG = logging.getLogger("obfmesh.auth")

ADMIN_KEY_HEADER = "X-API-Key"
DEFAULT_ADMIN_KEY_FILE = "/etc/obfmesh/admin.key"
ENV_ADMIN_KEY = "OBFMESH_ADMIN_KEY"
ENV_ADMIN_KEY_FILE = "OBFMESH_ADMIN_KEY_FILE"

MIN_ADMIN_KEY_LEN = 16

# Tokens are generated as secrets.token_urlsafe(32); reject anything else before touching the DB.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,255}$")

SECRET_PLACEHOLDER = MASKED  # SPEC invariant 1: secrets appear in reports as "<есть>"


# --------------------------------------------------------------------------- log redaction

_SECRET_VALUES: set[str] = set()
_SECRET_VALUES_LOCK = threading.Lock()
MIN_REDACTED_LEN = 8

# Credentials in a query string, on a command line and in a rendered config file - the three
# shapes a secret can take on its way into a log record.
_QUERY_SECRET_RE = re.compile(r"(?i)\b(token|key|api[-_]?key|password)=([^&\s\"'<>]+)")
_ARG_SECRET_RE = re.compile(r"(?i)(--(?:key|obfuscation[-_]key|token|password)[= ])(\S+)")
_WG_SECRET_RE = re.compile(r"(?i)\b((?:private|preshared)[-_ ]?key\s*[:=]\s*)(\S+)")
_CONF_SECRET_RE = re.compile(r"(?im)^([ \t]*(?:key|psk)[ \t]*=[ \t]*)(\S+)[ \t]*$")


def register_secret(value: str | None) -> None:
    """Add a literal value that must never appear in a log record."""
    if not value or len(value) < MIN_REDACTED_LEN:
        return
    with _SECRET_VALUES_LOCK:
        _SECRET_VALUES.add(value)


def redact(text: str) -> str:
    with _SECRET_VALUES_LOCK:
        values = tuple(_SECRET_VALUES)
    for value in values:
        text = text.replace(value, SECRET_PLACEHOLDER)
    text = _QUERY_SECRET_RE.sub(r"\1=" + SECRET_PLACEHOLDER, text)
    text = _ARG_SECRET_RE.sub(r"\1" + SECRET_PLACEHOLDER, text)
    text = _WG_SECRET_RE.sub(r"\1" + SECRET_PLACEHOLDER, text)
    text = _CONF_SECRET_RE.sub(r"\1" + SECRET_PLACEHOLDER, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Rewrites message and traceback of every record before a handler formats it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string is not a reason to drop the record
            return True
        redacted = redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()

        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        elif record.exc_info and record.exc_info[0] is not None:
            # Pre-rendering the traceback here is what lets us clean it: the formatter reuses
            # exc_text when it is already set.
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
        return True


def install_log_redaction() -> None:
    """Attach the filter to the handlers obfmesh and uvicorn records travel through."""
    log_filter = SecretRedactingFilter()
    loggers = [logging.getLogger()]
    loggers += [
        logging.getLogger(name)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "obfmesh")
    ]
    for logger in loggers:
        if not any(isinstance(f, SecretRedactingFilter) for f in logger.filters):
            logger.addFilter(log_filter)
        for handler in logger.handlers:
            if not any(isinstance(f, SecretRedactingFilter) for f in handler.filters):
                handler.addFilter(log_filter)


@dataclass(frozen=True)
class Principal:
    """Authenticated caller."""

    kind: Literal["admin", "client"]
    name: str
    client: "Client | None" = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"


class AdminKeyStore:
    """Admin key holder: environment wins, otherwise a file re-read when its mtime/size change.

    Re-reading on change means key rotation does not need a service restart.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._cached: str | None = None
        self._stamp: tuple[int, int] | None = None

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        return Path(os.environ.get(ENV_ADMIN_KEY_FILE, DEFAULT_ADMIN_KEY_FILE))

    def current(self) -> str | None:
        env_key = os.environ.get(ENV_ADMIN_KEY, "").strip()
        if env_key:
            return env_key
        return self._from_file()

    def _from_file(self) -> str | None:
        path = self.path
        try:
            st = os.stat(path)
        except OSError:
            with self._lock:
                self._cached = None
                self._stamp = None
            return None

        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if self._stamp == stamp and self._cached is not None:
                return self._cached

        try:
            raw = path.read_text(encoding="utf-8", errors="strict")
        except OSError as exc:
            LOG.error("cannot read admin key file %s: %s", path, exc.strerror)
            return None

        key = raw.strip()
        if len(key) < MIN_ADMIN_KEY_LEN:
            LOG.error(
                "admin key file %s holds fewer than %d characters, admin API stays disabled",
                path,
                MIN_ADMIN_KEY_LEN,
            )
            key = ""

        with self._lock:
            self._cached = key or None
            self._stamp = stamp
        return self._cached


ADMIN_KEYS = AdminKeyStore()


def _unauthorized(detail: str, scheme: str = "Bearer") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": scheme},
    )


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _check_admin_key(candidate: str) -> bool:
    expected = ADMIN_KEYS.current()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"admin key is not configured: create {ADMIN_KEYS.path} (mode 600) "
                f"or set {ENV_ADMIN_KEY}"
            ),
        )
    return secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def _client_by_token(token: str) -> "Client":
    # Shape check first: a malformed token never reaches the database.
    if not TOKEN_RE.match(token):
        raise _unauthorized("invalid bearer token")
    client = db.get_db().get_client_by_token(token)
    if client is None:
        raise _unauthorized("invalid bearer token")
    return client


def require_admin(request: Request) -> Principal:
    """FastAPI dependency: admin-only endpoints."""
    candidate = request.headers.get(ADMIN_KEY_HEADER)
    if candidate is None:
        if _bearer_token(request) is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="client tokens are not allowed on admin endpoints",
            )
        raise _unauthorized(f"missing {ADMIN_KEY_HEADER} header", scheme=ADMIN_KEY_HEADER)
    if not _check_admin_key(candidate):
        LOG.warning("rejected admin request from %s: bad api key", _peer(request))
        raise _unauthorized("invalid api key", scheme=ADMIN_KEY_HEADER)
    return Principal(kind="admin", name="admin")


def require_client(request: Request) -> Principal:
    """FastAPI dependency: endpoints a provisioned client calls with its own token."""
    token = _bearer_token(request)
    if token is None:
        raise _unauthorized("missing bearer token")
    client = _client_by_token(token)
    return Principal(kind="client", name=client.name, client=client)


def require_admin_or_client(request: Request) -> Principal:
    """FastAPI dependency: either principal (used by the SSE stream).

    Credentials are taken from headers only. The stream is read with fetch() in the UI and with
    curl on the router, so no query-string fallback is needed - and a key in a URL would end up
    in access logs.

    The router sends both headers at once (its curl config file carries the client token and,
    when configured, the admin key). A stale or rotated admin key must not cost it the event
    stream, so a failed admin check falls back to the bearer token instead of answering 401/503.
    """
    candidate = request.headers.get(ADMIN_KEY_HEADER)
    token = _bearer_token(request)
    if candidate is not None:
        try:
            if _check_admin_key(candidate):
                return Principal(kind="admin", name="admin")
        except HTTPException:
            # admin key is not configured on this server at all
            if token is None:
                raise
        if token is None:
            LOG.warning("rejected request from %s: bad api key", _peer(request))
            raise _unauthorized("invalid api key", scheme=ADMIN_KEY_HEADER)
        LOG.info("api key rejected for %s, falling back to the bearer token", _peer(request))
    if token is not None:
        return require_client(request)
    raise _unauthorized(f"missing {ADMIN_KEY_HEADER} header or bearer token")


def _peer(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"
