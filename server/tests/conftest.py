"""Общая обвязка тестов.

Каждый тест получает свой каталог состояния и свою базу: одиночки Database и
Orchestrator сбрасываются между тестами, иначе путь к базе прилипал бы к первому
обращению. По умолчанию включён OBFMESH_DRY_RUN — системные команды не
выполняются, поэтому набор гоняется без root и не на Linux тоже.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from obfmesh import db, orchestrator, wg  # noqa: E402

ENV_KEYS = (
    "OBFMESH_DB",
    "OBFMESH_ETC_DIR",
    "OBFMESH_RUN_DIR",
    "OBFMESH_LOG_DIR",
    "OBFMESH_DRY_RUN",
    "OBFMESH_MANAGE_INPUT",
    "OBFMESH_ADMIN_KEY",
    "OBFMESH_ADMIN_KEY_FILE",
    "OBFMESH_EXTERNAL_HOST",
    "OBFMESH_OBFUSCATOR_BIN",
    "OBFMESH_RECONCILE_INTERVAL",
    "OBFMESH_API_PORT",
    "OBFMESH_SKIP_MASKING_CHECK",
    "OBFMESH_STOP_PROCESSES",
)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Изолированное окружение: свои каталоги, своя база, dry-run."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    etc = tmp_path / "etc"
    run = tmp_path / "run"
    logs = tmp_path / "log"
    for path in (etc, run, logs):
        path.mkdir(mode=0o700)

    monkeypatch.setenv("OBFMESH_DB", str(tmp_path / "obfmesh.db"))
    monkeypatch.setenv("OBFMESH_ETC_DIR", str(etc))
    monkeypatch.setenv("OBFMESH_RUN_DIR", str(run))
    monkeypatch.setenv("OBFMESH_LOG_DIR", str(logs))
    monkeypatch.setenv("OBFMESH_DRY_RUN", "1")
    monkeypatch.setenv("OBFMESH_EXTERNAL_HOST", "45.136.127.10")
    monkeypatch.setenv("OBFMESH_RECONCILE_INTERVAL", "0")

    db.reset_db()
    orchestrator.reset_orchestrator()
    try:
        yield tmp_path
    finally:
        orchestrator.reset_orchestrator()
        db.reset_db()


@pytest.fixture
def database(env):
    return db.get_db()


@pytest.fixture
def orch(env):
    return orchestrator.get_orchestrator()


@pytest.fixture
def live(env, monkeypatch, tmp_path):
    """Как env, но без dry-run и с фейковым обфускатором.

    Нужен тестам, которые проверяют работу с настоящими процессами: подбор по
    pid-файлу, перезапуск, остановку.
    """
    monkeypatch.setenv("OBFMESH_DRY_RUN", "0")

    binary = tmp_path / "fake-obfuscator"
    binary.write_text(
        "#!/bin/sh\n"
        "# Заглушка wg-obfuscator: держится живой и понимает --help.\n"
        'case "$1" in --help|-h) echo \'  -a, --masking=<type>  masking type\'; exit 0 ;; esac\n'
        "while :; do sleep 1; done\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    monkeypatch.setenv("OBFMESH_OBFUSCATOR_BIN", str(binary))

    # /proc есть только на Linux; на macOS то же самое даёт ps.
    if not os.path.isdir("/proc"):
        def _cmdline(pid: int) -> list[str]:
            result = subprocess.run(
                ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True
            )
            return result.stdout.strip().split() if result.returncode == 0 else []

        monkeypatch.setattr(orchestrator, "_read_cmdline", _cmdline)

    db.reset_db()
    orchestrator.reset_orchestrator()
    database = db.get_db()
    database.update_settings({"obfuscator_bin": str(binary), "spokes": 2})

    created: list[orchestrator.Orchestrator] = []
    instance = orchestrator.get_orchestrator()
    created.append(instance)
    try:
        yield instance, str(binary)
    finally:
        for item in created:
            try:
                item.teardown_processes()
            except Exception:  # noqa: BLE001 - уборка не должна ронять тест
                pass
        orchestrator.reset_orchestrator()
        db.reset_db()


@pytest.fixture
def dry_run_disabled(monkeypatch):
    monkeypatch.setenv("OBFMESH_DRY_RUN", "0")
    assert not wg.dry_run()
