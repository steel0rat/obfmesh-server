"""Обновление схемы на боевой базе: записи прежних версий обязаны выжить.

Настройка agg_mode исчезла, но колонка в таблице осталась — она NOT NULL без
значения по умолчанию, а её удаление означало бы пересборку таблицы. Миграция
003 схлопывает все прежние значения ('single', 'ecmp', 'teql') в одно.
"""

from __future__ import annotations

import sqlite3

import pytest

from obfmesh import db
from obfmesh.models import FROZEN_AGG_MODE_COLUMN, Settings


def _legacy_database(path: str, agg_mode: str, *, version: int = 2) -> None:
    """Собрать базу прежней версии её же миграциями, без нынешнего кода.

    Строка настроек пишется вручную: `Database._ensure_settings_row()` нынешней
    версии положил бы уже новое значение, и проверять было бы нечего.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        for step in range(version):
            db._MIGRATIONS[step](conn)  # noqa: SLF001
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute(
            """
            INSERT INTO settings
                (id, spokes, agg_mode, masking, mtu, external_host, port_base,
                 obfuscation_key, obfuscator_bin, config_version, updated_at)
            VALUES (1, 3, ?, 'STUN', 1400, '45.136.127.10', 48200,
                    'legacy-obfuscation-key-1234', '/usr/local/bin/wg-obfuscator', 7, '2026-08-01T00:00:00+00:00')
            """,
            (agg_mode,),
        )
        conn.execute(
            "INSERT INTO clients (name, slot, token_hash, token_prefix, created_at)"
            " VALUES ('router', 0, 'deadbeef', 'abc', '2026-08-01T00:00:00+00:00')"
        )
    finally:
        conn.close()


def _stored_agg_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("SELECT agg_mode FROM settings WHERE id = 1").fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("legacy_mode", ["single", "ecmp", "teql"])
def test_existing_rows_survive_the_upgrade(env, legacy_mode):
    path = str(env / "legacy.db")
    _legacy_database(path, legacy_mode)

    database = db.Database(path)
    settings = database.get_settings()

    # Всё, что не про агрегацию, дошло без потерь.
    assert settings.spokes == 3
    assert settings.mtu == 1400
    assert settings.external_host == "45.136.127.10"
    assert settings.config_version == 7
    assert database.get_client("router") is not None
    # Настройки agg_mode больше нет ни в модели, ни в ответе API.
    assert not hasattr(settings, "agg_mode")
    assert "agg_mode" not in settings.to_api()


@pytest.mark.parametrize("legacy_mode", ["ecmp", "teql"])
def test_broken_modes_collapse_to_the_frozen_value(env, legacy_mode):
    """ecmp и teql доказанно не работают — в базе они не остаются."""
    path = str(env / "legacy.db")
    _legacy_database(path, legacy_mode)

    db.Database(path).get_settings()

    assert _stored_agg_mode(path) == FROZEN_AGG_MODE_COLUMN


def test_migration_is_idempotent(env):
    path = str(env / "legacy.db")
    _legacy_database(path, "teql")

    db.Database(path).get_settings()
    first = _stored_agg_mode(path)
    # Второе открытие уже не находит миграций: user_version дошёл до текущего.
    db.Database(path).get_settings()

    assert _stored_agg_mode(path) == first == FROZEN_AGG_MODE_COLUMN
    conn = sqlite3.connect(path)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == db.SCHEMA_VERSION
    finally:
        conn.close()


def test_fresh_database_fills_the_frozen_column(env):
    """Колонка NOT NULL: новая база обязана записать в неё что-то валидное."""
    database = db.get_db()
    database.init()

    assert _stored_agg_mode(database.path) == FROZEN_AGG_MODE_COLUMN
    assert isinstance(database.get_settings(), Settings)
