"""Работа с процессами обфускаторов на фейковом бинарнике: подбор, надзор, остановка."""

from __future__ import annotations

import os
import time

from obfmesh.orchestrator import ReconcileReport, _pid_alive, _read_pidfile


def _start(orch, index=1):
    settings = orch.db.get_settings()
    spoke, _ = orch.db.ensure_spoke(index, settings.port_base)
    report = ReconcileReport()
    orch._reconcile_obfuscator(spoke, settings, report)  # noqa: SLF001
    return report


def test_start_writes_a_pid_file_of_a_live_process(live):
    orch, binary = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))
    assert pid and _pid_alive(pid)
    assert orch._procs[1].binary == binary  # noqa: SLF001


def test_second_reconcile_does_not_restart(live):
    orch, _ = live
    _start(orch, 1)
    first = _read_pidfile(orch.pid_path(1))
    _start(orch, 1)
    assert _read_pidfile(orch.pid_path(1)) == first


def test_supervisor_adopts_instead_of_starting_a_second_one(live):
    """Блокер: пустой self._procs при живом процессе приводил ко второму обфускатору."""
    orch, _ = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))

    # Имитируем сбой reconcile до запуска обфускатора: процесс жив, таблицы нет.
    orch._procs.clear()  # noqa: SLF001

    orch._check_processes()  # noqa: SLF001

    assert _read_pidfile(orch.pid_path(1)) == pid, "надзор переписал pid-файл новым процессом"
    assert _pid_alive(pid)
    assert orch._procs[1].pid == pid  # noqa: SLF001
    assert orch._procs[1].adopted is True  # noqa: SLF001


def test_start_refuses_to_overwrite_a_live_pid_file(live):
    orch, _ = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))
    orch._procs.clear()  # noqa: SLF001

    settings = orch.db.get_settings()
    report = ReconcileReport()
    orch._start_obfuscator(1, settings, report)  # noqa: SLF001

    assert _read_pidfile(orch.pid_path(1)) == pid
    assert any("уже работает" in item for item in report.actions)


def test_dead_process_is_restarted_with_a_new_pid(live):
    orch, _ = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))
    os.kill(pid, 9)
    deadline = time.monotonic() + 5
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)

    orch._check_processes()  # noqa: SLF001
    fresh = _read_pidfile(orch.pid_path(1))
    assert fresh and fresh != pid
    assert _pid_alive(fresh)


def test_teardown_stops_everything_and_removes_pid_files(live):
    orch, _ = live
    _start(orch, 1)
    _start(orch, 2)
    pids = [_read_pidfile(orch.pid_path(i)) for i in (1, 2)]

    stopped = orch.teardown_processes()
    assert len(stopped) == 2
    for index, pid in zip((1, 2), pids):
        assert not os.path.exists(orch.pid_path(index))
        assert not _pid_alive(pid)


def test_teardown_finds_processes_of_a_previous_run(live):
    """`obfmesh-ctl teardown` работает в новом процессе, где self._procs пуст."""
    orch, _ = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))
    orch._procs.clear()  # noqa: SLF001

    stopped = orch.teardown_processes()
    assert len(stopped) == 1
    assert not _pid_alive(pid)


def test_config_change_restarts_the_obfuscator(live):
    orch, _ = live
    _start(orch, 1)
    pid = _read_pidfile(orch.pid_path(1))

    orch.db.update_settings({"mtu": 1380, "key": "another-obfuscation-key-123456"})
    _start(orch, 1)

    fresh = _read_pidfile(orch.pid_path(1))
    # MTU в конфиг обфускатора не входит, а ключ входит: он и вызывает перезапуск
    assert fresh != pid
    assert not _pid_alive(pid)


def test_masking_check_blocks_a_binary_without_support(live, tmp_path, monkeypatch):
    """Без подтверждённой маскировки луч не поднимается: провайдер режет поток."""
    orch, _ = live
    naked = tmp_path / "naked-obfuscator"
    naked.write_text("#!/bin/sh\necho 'usage: obfuscator'\nwhile :; do sleep 1; done\n", encoding="utf-8")
    naked.chmod(0o755)
    orch.db.update_settings({"obfuscator_bin": str(naked)})

    settings = orch.db.get_settings()
    report = ReconcileReport()
    try:
        orch._start_obfuscator(1, settings, report)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 - проверяем текст сообщения
        assert "masking" in str(exc)
    else:
        raise AssertionError("обфускатор без поддержки masking был запущен")


def test_instant_death_is_reported(live, tmp_path):
    orch, _ = live
    dying = tmp_path / "dying-obfuscator"
    dying.write_text(
        "#!/bin/sh\ncase \"$1\" in --help) echo masking; exit 0 ;; esac\nexit 3\n", encoding="utf-8"
    )
    dying.chmod(0o755)
    orch.db.update_settings({"obfuscator_bin": str(dying)})

    settings = orch.db.get_settings()
    try:
        orch._start_obfuscator(1, settings, ReconcileReport())  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        assert "умер" in str(exc)
    else:
        raise AssertionError("мгновенно умерший обфускатор посчитали запущенным")
