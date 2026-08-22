"""Состояние obfmesh в SQLite: схема, миграции, CRUD.

Путь к базе — /var/lib/obfmesh/obfmesh.db, переопределяется переменной OBFMESH_DB.
База и её WAL-файлы создаются с правами 600, каталог — 700: внутри лежат приватные
ключи WireGuard и хеши клиентских токенов.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Sequence

from . import wg
from .models import (
    FROZEN_AGG_MODE_COLUMN,
    SPOKE_MAX,
    SPOKE_MIN,
    Client,
    ClientKey,
    CLIENT_NAME_RE,
    CLIENT_SLOT_MAX,
    Masking,
    ObfmeshError,
    Settings,
    Spoke,
    ValidationError,
    client_address,
    server_address,
    server_obf_port,
    spoke_network,
    validate_client_slot,
    validate_spoke_index,
    wg_port,
)

log = logging.getLogger("obfmesh.db")

DEFAULT_DB_PATH = "/var/lib/obfmesh/obfmesh.db"
DEFAULT_OBFUSCATOR_BIN = "/usr/local/bin/wg-obfuscator"

SCHEMA_VERSION = 3

# Поля, изменение которых меняет клиентский бандл и обязано поднять config_version.
_BUNDLE_FIELDS = frozenset(
    {"spokes", "masking", "mtu", "external_host", "port_base", "obfuscation_key"}
)

_SETTINGS_FIELDS = frozenset(_BUNDLE_FIELDS | {"obfuscator_bin"})

TOKEN_ID_LEN = 8


class NotFound(ObfmeshError):
    """Запрошенной записи нет."""


class Conflict(ObfmeshError):
    """Запись с таким ключом уже есть либо кончились свободные слоты."""


def db_path() -> str:
    return os.environ.get("OBFMESH_DB", "").strip() or DEFAULT_DB_PATH


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_id(token: str) -> str:
    """Публичный идентификатор токена: 8 hex от sha256, не часть самого токена."""
    return hash_token(token)[:TOKEN_ID_LEN]


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def generate_obfuscation_key() -> str:
    # token_urlsafe даёт [A-Za-z0-9_-] — безопасно для конфига wg-obfuscator.
    return secrets.token_urlsafe(24)


class Database:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or db_path()
        self._lock = threading.RLock()
        self._change_cond = threading.Condition()
        self._initialized = False

    # --- служебное ---------------------------------------------------------

    def _prepare_files(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        if not os.path.exists(self.path):
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    def _fix_sidecar_permissions(self) -> None:
        for suffix in ("-wal", "-shm"):
            sidecar = self.path + suffix
            if os.path.exists(sidecar):
                with contextlib.suppress(OSError):
                    os.chmod(sidecar, 0o600)

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextlib.contextmanager
    def _tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        self.init()
        conn = self._new_conn()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        self._fix_sidecar_permissions()

    @contextlib.contextmanager
    def _ro(self) -> Iterator[sqlite3.Connection]:
        self.init()
        conn = self._new_conn()
        try:
            yield conn
        finally:
            conn.close()

    # --- инициализация и миграции -----------------------------------------

    def init(self) -> None:
        """Создать файл, накатить миграции, завести строку настроек. Идемпотентно."""
        with self._lock:
            if self._initialized:
                return
            self._prepare_files()
            conn = self._new_conn()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version > SCHEMA_VERSION:
                    raise ObfmeshError(
                        f"схема базы новее кода: user_version={version}, поддерживается {SCHEMA_VERSION}"
                    )
                for step in range(version, SCHEMA_VERSION):
                    migration = _MIGRATIONS[step]
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        # Версия перечитывается под блокировкой: другой процесс мог
                        # накатить эту же миграцию, пока мы ждали запись.
                        if int(conn.execute("PRAGMA user_version").fetchone()[0]) != step:
                            conn.execute("ROLLBACK")
                            continue
                        migration(conn)
                        conn.execute(f"PRAGMA user_version = {step + 1}")
                    except BaseException:
                        conn.execute("ROLLBACK")
                        raise
                    conn.execute("COMMIT")
                    log.info("миграция схемы %s -> %s применена", step, step + 1)
                self._ensure_settings_row(conn)
            finally:
                conn.close()
            self._fix_sidecar_permissions()
            self._initialized = True

    def _ensure_settings_row(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT 1 FROM settings WHERE id = 1").fetchone()
        if row:
            return
        defaults = Settings(
            external_host=os.environ.get("OBFMESH_EXTERNAL_HOST", "").strip() or _detect_external_host(),
            obfuscation_key=generate_obfuscation_key(),
            obfuscator_bin=os.environ.get("OBFMESH_OBFUSCATOR_BIN", "").strip() or DEFAULT_OBFUSCATOR_BIN,
            updated_at=utcnow(),
        )
        defaults.validate()
        conn.execute("BEGIN IMMEDIATE")
        try:
            # agg_mode is no longer a setting; the column is kept NOT NULL and
            # filled with the frozen legacy value (see _migration_003).
            conn.execute(
                """
                INSERT OR IGNORE INTO settings
                    (id, spokes, agg_mode, masking, mtu, external_host, port_base,
                     obfuscation_key, obfuscator_bin, config_version, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    defaults.spokes,
                    FROZEN_AGG_MODE_COLUMN,
                    defaults.masking.value,
                    defaults.mtu,
                    defaults.external_host,
                    defaults.port_base,
                    defaults.obfuscation_key,
                    defaults.obfuscator_bin,
                    defaults.updated_at,
                ),
            )
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        log.info("создана строка настроек по умолчанию (лучей: %s)", defaults.spokes)

    # --- настройки ---------------------------------------------------------

    def get_settings(self) -> Settings:
        with self._ro() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        if row is None:
            raise NotFound("строка настроек отсутствует")
        return _row_to_settings(row)

    def update_settings(self, patch: dict[str, Any]) -> tuple[Settings, bool]:
        """Применить изменения. Возвращает (настройки, было_ли_изменение).

        Повторный вызов с теми же значениями ничего не пишет и не двигает config_version.
        """
        unknown = set(patch) - _SETTINGS_FIELDS - {"key"}
        if unknown:
            raise ValidationError(f"неизвестные поля настроек: {sorted(unknown)}")
        normalized = dict(patch)
        if "key" in normalized:  # SPEC называет ключ в /api/settings полем key
            normalized["obfuscation_key"] = normalized.pop("key")

        with self._tx() as conn:
            row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
            if row is None:
                raise NotFound("строка настроек отсутствует")
            current = _row_to_settings(row)

            updated = Settings(
                spokes=int(normalized.get("spokes", current.spokes)),
                masking=Masking(normalized.get("masking", current.masking)),
                mtu=int(normalized.get("mtu", current.mtu)),
                external_host=str(normalized.get("external_host", current.external_host)).strip(),
                port_base=int(normalized.get("port_base", current.port_base)),
                obfuscation_key=str(normalized.get("obfuscation_key", current.obfuscation_key)),
                obfuscator_bin=str(normalized.get("obfuscator_bin", current.obfuscator_bin)),
                config_version=current.config_version,
                updated_at=current.updated_at,
            )
            updated.validate()

            changed_fields = [
                name
                for name in sorted(_SETTINGS_FIELDS)
                if getattr(updated, name) != getattr(current, name)
            ]
            if not changed_fields:
                return current, False

            if _BUNDLE_FIELDS.intersection(changed_fields):
                updated.config_version = current.config_version + 1
            updated.updated_at = utcnow()

            conn.execute(
                """
                UPDATE settings SET spokes = ?, masking = ?, mtu = ?,
                       external_host = ?, port_base = ?, obfuscation_key = ?,
                       obfuscator_bin = ?, config_version = ?, updated_at = ?
                 WHERE id = 1
                """,
                (
                    updated.spokes,
                    updated.masking.value,
                    updated.mtu,
                    updated.external_host,
                    updated.port_base,
                    updated.obfuscation_key,
                    updated.obfuscator_bin,
                    updated.config_version,
                    updated.updated_at,
                ),
            )
        # obfuscation_key в списке изменённых полей выводится как имя, без значения.
        log.info("настройки изменены: %s (config_version=%s)", changed_fields, updated.config_version)
        self._notify(updated.config_version)
        return updated, True

    def get_config_version(self) -> int:
        with self._ro() as conn:
            row = conn.execute("SELECT config_version FROM settings WHERE id = 1").fetchone()
        if row is None:
            raise NotFound("строка настроек отсутствует")
        return int(row["config_version"])

    def bump_config_version(self) -> int:
        with self._tx() as conn:
            conn.execute("UPDATE settings SET config_version = config_version + 1, updated_at = ? WHERE id = 1", (utcnow(),))
            version = int(conn.execute("SELECT config_version FROM settings WHERE id = 1").fetchone()[0])
        self._notify(version)
        return version

    # --- уведомление об изменениях (для SSE /api/events) -------------------

    def _notify(self, version: int) -> None:
        log.debug("config_version=%s, будим подписчиков", version)
        with self._change_cond:
            self._change_cond.notify_all()

    def wait_for_change(self, since: int, timeout: float = 25.0) -> int | None:
        """Дождаться роста config_version. Возвращает новую версию либо None по таймауту.

        Работает в пределах одного процесса: событие поднимает тот же процесс,
        который менял настройки. Для нескольких воркеров uvicorn нужен опрос.
        """
        current = self.get_config_version()
        if current != since:
            return current
        with self._change_cond:
            self._change_cond.wait(timeout=timeout)
        current = self.get_config_version()
        return current if current != since else None

    # --- лучи --------------------------------------------------------------

    def get_spoke(self, index: int) -> Spoke | None:
        validate_spoke_index(index)
        with self._ro() as conn:
            row = conn.execute("SELECT * FROM spokes WHERE idx = ?", (index,)).fetchone()
        return _row_to_spoke(row) if row else None

    def list_spokes(self, *, include_disabled: bool = True) -> list[Spoke]:
        query = "SELECT * FROM spokes"
        if not include_disabled:
            query += " WHERE enabled = 1"
        query += " ORDER BY idx"
        with self._ro() as conn:
            rows = conn.execute(query).fetchall()
        return [_row_to_spoke(row) for row in rows]

    def ensure_spoke(self, index: int, port_base: int) -> tuple[Spoke, list[str]]:
        """Создать при отсутствии, включить и выправить порт обфускатора.

        Ключи уже созданного луча не перегенерируются: выключение и повторное
        включение луча не рвёт бандлы у клиентов.
        """
        validate_spoke_index(index)
        changes: list[str] = []
        expected_obf_port = server_obf_port(index, port_base)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM spokes WHERE idx = ?", (index,)).fetchone()
            if row is None:
                private, public = wg.keypair()
                created_at = utcnow()
                conn.execute(
                    """
                    INSERT INTO spokes
                        (idx, listen_port, obf_port, server_ip, subnet, private_key,
                         public_key, enabled, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
                    """,
                    (
                        index,
                        wg_port(index),
                        expected_obf_port,
                        server_address(index),
                        str(spoke_network(index)),
                        private,
                        public,
                        created_at,
                    ),
                )
                changes.append(f"луч {index}: создан, ключи сгенерированы")
            else:
                existing = _row_to_spoke(row)
                if not existing.enabled:
                    conn.execute("UPDATE spokes SET enabled = 1 WHERE idx = ?", (index,))
                    changes.append(f"луч {index}: включён")
                if existing.obf_port != expected_obf_port:
                    conn.execute("UPDATE spokes SET obf_port = ? WHERE idx = ?", (expected_obf_port, index))
                    changes.append(f"луч {index}: порт обфускатора {existing.obf_port} -> {expected_obf_port}")
                if existing.listen_port != wg_port(index):
                    conn.execute("UPDATE spokes SET listen_port = ? WHERE idx = ?", (wg_port(index), index))
                    changes.append(f"луч {index}: порт WireGuard выправлен на {wg_port(index)}")
            spoke = _row_to_spoke(conn.execute("SELECT * FROM spokes WHERE idx = ?", (index,)).fetchone())
        return spoke, changes

    def set_spoke_enabled(self, index: int, enabled: bool) -> bool:
        validate_spoke_index(index)
        with self._tx() as conn:
            row = conn.execute("SELECT enabled FROM spokes WHERE idx = ?", (index,)).fetchone()
            if row is None:
                return False
            if bool(row["enabled"]) == enabled:
                return False
            conn.execute("UPDATE spokes SET enabled = ? WHERE idx = ?", (1 if enabled else 0, index))
        return True

    # --- клиенты -----------------------------------------------------------

    def list_clients(self) -> list[Client]:
        with self._ro() as conn:
            rows = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
            clients = [_row_to_client(row) for row in rows]
            for client in clients:
                client.keys = _load_client_keys(conn, client.id)
        return clients

    def get_client(self, name: str) -> Client | None:
        with self._ro() as conn:
            row = conn.execute("SELECT * FROM clients WHERE name = ?", (name,)).fetchone()
            if row is None:
                return None
            client = _row_to_client(row)
            client.keys = _load_client_keys(conn, client.id)
        return client

    def get_client_by_token(self, token: str) -> Client | None:
        """Поиск по sha256 токена: сам токен в базе не хранится."""
        if not token:
            return None
        with self._ro() as conn:
            row = conn.execute("SELECT * FROM clients WHERE token_hash = ?", (hash_token(token),)).fetchone()
            if row is None:
                return None
            client = _row_to_client(row)
            client.keys = _load_client_keys(conn, client.id)
        return client

    def create_client(self, name: str) -> tuple[Client, str]:
        """Создать клиента, ключи на все возможные лучи 1..10 и токен.

        Токен возвращается один раз: в базе лежит только его sha256. Ключи делаются
        сразу на все лучи, поэтому увеличение их числа не требует новых ключей и не
        ломает уже выданные бандлы.
        """
        name = (name or "").strip()
        if not CLIENT_NAME_RE.match(name):
            raise ValidationError(
                "имя клиента: 1-64 символа [A-Za-z0-9._-], первый символ — буква или цифра"
            )
        token = generate_token()
        created_at = utcnow()
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM clients WHERE name = ?", (name,)).fetchone():
                raise Conflict(f"клиент {name!r} уже существует")
            used = {int(r["slot"]) for r in conn.execute("SELECT slot FROM clients")}
            slot = next((s for s in range(0, CLIENT_SLOT_MAX + 1) if s not in used), None)
            if slot is None:
                raise Conflict(f"свободные слоты кончились (максимум {CLIENT_SLOT_MAX + 1} клиентов)")
            cursor = conn.execute(
                "INSERT INTO clients (name, slot, token_hash, token_prefix, token_id, created_at)"
                " VALUES (?, ?, ?, '', ?, ?)",
                (name, slot, hash_token(token), token_id(token), created_at),
            )
            client_id = int(cursor.lastrowid)
            _insert_client_keys(conn, client_id, slot, range(SPOKE_MIN, SPOKE_MAX + 1))
            conn.execute("UPDATE settings SET config_version = config_version + 1, updated_at = ? WHERE id = 1", (created_at,))
            version = int(conn.execute("SELECT config_version FROM settings WHERE id = 1").fetchone()[0])
            row = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
            client = _row_to_client(row)
            client.keys = _load_client_keys(conn, client_id)
        log.info("создан клиент %s (слот %s, config_version=%s)", name, slot, version)
        self._notify(version)
        return client, token

    def rotate_client_token(self, name: str) -> str:
        """Выдать клиенту новый токен. Ключи и адреса не меняются."""
        token = generate_token()
        with self._tx() as conn:
            row = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
            if row is None:
                raise NotFound(f"клиент {name!r} не найден")
            conn.execute(
                "UPDATE clients SET token_hash = ?, token_prefix = '', token_id = ? WHERE id = ?",
                (hash_token(token), token_id(token), int(row["id"])),
            )
        log.info("токен клиента %s заменён", name)
        return token

    def delete_client(self, name: str) -> bool:
        with self._tx() as conn:
            row = conn.execute("SELECT id FROM clients WHERE name = ?", (name,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM clients WHERE id = ?", (int(row["id"]),))
            conn.execute("UPDATE settings SET config_version = config_version + 1, updated_at = ? WHERE id = 1", (utcnow(),))
            version = int(conn.execute("SELECT config_version FROM settings WHERE id = 1").fetchone()[0])
        log.info("клиент %s удалён (config_version=%s)", name, version)
        self._notify(version)
        return True

    def ensure_client_keys(self, indexes: Iterable[int] | None = None) -> list[str]:
        """Догенерировать недостающие ключи клиентов для указанных лучей.

        Нужна для баз, заведённых до расширения диапазона лучей: клиент никогда
        не должен получить бандл с лучом без ключей (SPEC, инвариант 5).
        """
        wanted = sorted({validate_spoke_index(i) for i in (indexes or range(SPOKE_MIN, SPOKE_MAX + 1))})
        changes: list[str] = []
        with self._tx() as conn:
            for row in conn.execute("SELECT id, name, slot FROM clients ORDER BY name").fetchall():
                client_id, name, slot = int(row["id"]), row["name"], int(row["slot"])
                existing = {
                    int(r["spoke_idx"])
                    for r in conn.execute("SELECT spoke_idx FROM client_keys WHERE client_id = ?", (client_id,))
                }
                missing = [i for i in wanted if i not in existing]
                if missing:
                    _insert_client_keys(conn, client_id, slot, missing)
                    changes.append(f"клиент {name}: сгенерированы ключи для лучей {missing}")
        return changes

    def client_peers(self, index: int) -> dict[str, dict[str, Any]]:
        """Пиры луча: публичный ключ -> {client, address}.

        Address is the client address on this spoke and nothing else: it is the
        whole of the peer's allowed-ips, so no address of another spoke and no
        address shared between spokes can leak in.
        """
        validate_spoke_index(index)
        with self._ro() as conn:
            rows = conn.execute(
                """
                SELECT c.name AS name,
                       k.public_key AS public_key, k.address AS address
                  FROM client_keys k JOIN clients c ON c.id = k.client_id
                 WHERE k.spoke_idx = ?
                 ORDER BY c.name
                """,
                (index,),
            ).fetchall()
        return {
            row["public_key"]: {
                "client": row["name"],
                "address": row["address"],
            }
            for row in rows
        }


# --- отображение строк в модели --------------------------------------------


def _row_to_settings(row: sqlite3.Row) -> Settings:
    # agg_mode is deliberately not read: the column survives for one release so
    # that a rollback still finds a value its enum accepts (_migration_003).
    return Settings(
        spokes=int(row["spokes"]),
        masking=Masking(row["masking"]),
        mtu=int(row["mtu"]),
        external_host=row["external_host"] or "",
        port_base=int(row["port_base"]),
        obfuscation_key=row["obfuscation_key"],
        obfuscator_bin=row["obfuscator_bin"],
        config_version=int(row["config_version"]),
        updated_at=row["updated_at"] or "",
    )


def _row_to_spoke(row: sqlite3.Row) -> Spoke:
    return Spoke(
        index=int(row["idx"]),
        listen_port=int(row["listen_port"]),
        obf_port=int(row["obf_port"]),
        server_ip=row["server_ip"],
        subnet=row["subnet"],
        private_key=row["private_key"],
        public_key=row["public_key"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"] or "",
    )


def _row_to_client(row: sqlite3.Row) -> Client:
    return Client(
        id=int(row["id"]),
        name=row["name"],
        slot=int(row["slot"]),
        token_id=row["token_id"] or "",
        token_hash=row["token_hash"],
        created_at=row["created_at"] or "",
    )


def _load_client_keys(conn: sqlite3.Connection, client_id: int) -> dict[int, ClientKey]:
    rows = conn.execute(
        "SELECT spoke_idx, private_key, public_key, address FROM client_keys WHERE client_id = ? ORDER BY spoke_idx",
        (client_id,),
    ).fetchall()
    return {
        int(row["spoke_idx"]): ClientKey(
            spoke_index=int(row["spoke_idx"]),
            private_key=row["private_key"],
            public_key=row["public_key"],
            address=row["address"],
        )
        for row in rows
    }


def _insert_client_keys(conn: sqlite3.Connection, client_id: int, slot: int, indexes: Iterable[int]) -> None:
    validate_client_slot(slot)
    for index in indexes:
        validate_spoke_index(index)
        private, public = wg.keypair()
        conn.execute(
            "INSERT OR IGNORE INTO client_keys (client_id, spoke_idx, private_key, public_key, address) VALUES (?, ?, ?, ?, ?)",
            (client_id, index, private, public, client_address(index, slot)),
        )


# --- миграции ---------------------------------------------------------------


def _migration_001(conn: sqlite3.Connection) -> None:
    # Каждый оператор выполняется отдельно: executescript закрыл бы начатую транзакцию.
    for statement in _SCHEMA_001:
        conn.execute(statement)


_SCHEMA_001: Sequence[str] = (
    """
    CREATE TABLE settings (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        spokes          INTEGER NOT NULL,
        agg_mode        TEXT    NOT NULL,
        masking         TEXT    NOT NULL,
        mtu             INTEGER NOT NULL,
        external_host   TEXT    NOT NULL DEFAULT '',
        port_base       INTEGER NOT NULL,
        obfuscation_key TEXT    NOT NULL,
        obfuscator_bin  TEXT    NOT NULL,
        config_version  INTEGER NOT NULL DEFAULT 1,
        updated_at      TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE spokes (
        idx         INTEGER PRIMARY KEY,
        listen_port INTEGER NOT NULL,
        obf_port    INTEGER NOT NULL,
        server_ip   TEXT    NOT NULL,
        subnet      TEXT    NOT NULL,
        private_key TEXT    NOT NULL,
        public_key  TEXT    NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE clients (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL UNIQUE,
        slot         INTEGER NOT NULL UNIQUE,
        token_hash   TEXT    NOT NULL UNIQUE,
        token_prefix TEXT    NOT NULL DEFAULT '',
        created_at   TEXT    NOT NULL
    )
    """,
    """
    CREATE TABLE client_keys (
        client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
        spoke_idx   INTEGER NOT NULL,
        private_key TEXT    NOT NULL,
        public_key  TEXT    NOT NULL,
        address     TEXT    NOT NULL,
        PRIMARY KEY (client_id, spoke_idx)
    )
    """,
    "CREATE INDEX idx_client_keys_spoke ON client_keys (spoke_idx)",
)


def _migration_002(conn: sqlite3.Connection) -> None:
    """token_prefix хранил первые 8 символов живого токена и уходил в /api/clients.

    Колонка не удаляется (её могли прочитать сторонние копии схемы), но значение
    затирается, а вместо него заводится token_id — префикс sha256, не секрет.
    """
    conn.execute("ALTER TABLE clients ADD COLUMN token_id TEXT NOT NULL DEFAULT ''")
    conn.execute(f"UPDATE clients SET token_id = substr(token_hash, 1, {TOKEN_ID_LEN})")
    conn.execute("UPDATE clients SET token_prefix = ''")


def _migration_003(conn: sqlite3.Connection) -> None:
    """Aggregation modes are gone; the agg_mode column is frozen, not dropped.

    Rows written by earlier releases hold 'single', 'ecmp' or 'teql'. Both ecmp
    and teql were measured to be dead ends (teql cannot even send over a NOARP
    wireguard link), so every row collapses onto the one value a rolled-back
    1.1.x server still parses: 'single'.

    The column itself stays. It is NOT NULL without a default, dropping it would
    rebuild the table, and keeping it means a downgrade still parses the row.
    Nothing reads it any more - see _row_to_settings().
    """
    conn.execute(
        "UPDATE settings SET agg_mode = ? WHERE agg_mode <> ?",
        (FROZEN_AGG_MODE_COLUMN, FROZEN_AGG_MODE_COLUMN),
    )


_MIGRATIONS: Sequence[Any] = (_migration_001, _migration_002, _migration_003)


# --- одиночка ---------------------------------------------------------------

_instance: Database | None = None
_instance_lock = threading.Lock()


def get_db() -> Database:
    """Общий на процесс экземпляр. Путь берётся из OBFMESH_DB при первом обращении."""
    global _instance
    with _instance_lock:
        path = db_path()
        if _instance is None or _instance.path != path:
            _instance = Database(path)
        return _instance


def reset_db() -> None:
    """Сбросить одиночку — нужно тестам, меняющим OBFMESH_DB."""
    global _instance
    with _instance_lock:
        _instance = None


def _detect_external_host() -> str:
    """Внешний адрес по маршруту по умолчанию. UDP-сокет пакетов не шлёт."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 53))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()
