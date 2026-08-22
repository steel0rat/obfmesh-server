"""Клиентские shell-тесты запускаются вместе с серверными.

Вторая половина обоих сломанных режимов живёт в
openwrt/obfmesh/files/usr/lib/obfmesh/lib.sh, и её нечем было проверить.
openwrt/obfmesh/tests/run.sh — POSIX-sh набор поверх заглушек iproute2/uci;
здесь он просто выполняется, чтобы `pytest tests -q` покрывал обе стороны.

Пропускается, если под рукой нет ни dash, ни busybox: набор рассчитан на
BusyBox ash, но bash подставлять нельзя — он проглотит башизмы, которых на
роутере не будет.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SUITE = Path(__file__).resolve().parents[2] / "openwrt" / "obfmesh" / "tests" / "run.sh"


def _shell() -> list[str] | None:
    dash = shutil.which("dash")
    if dash:
        return [dash]
    busybox = shutil.which("busybox")
    if busybox:
        return [busybox, "ash"]
    # /bin/sh годится, только если это не bash: башизмы должны падать, а не работать.
    sh = shutil.which("sh")
    if sh and "bash" not in Path(sh).resolve().name:
        return [sh]
    return None


@pytest.mark.skipif(not SUITE.is_file(), reason="клиентский набор недоступен")
def test_client_shell_suite() -> None:
    shell = _shell()
    if shell is None:
        pytest.skip("нет POSIX-совместимого sh (нужен dash или busybox)")

    result = subprocess.run(
        [*shell, str(SUITE)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(SUITE.parent),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 failed" in result.stdout, result.stdout


@pytest.mark.skipif(not SUITE.is_file(), reason="клиентский набор недоступен")
def test_client_scripts_are_posix_sh() -> None:
    """`dash -n` по каждому скрипту клиента: башизм на роутере не запустится."""
    shell = _shell()
    if shell is None:
        pytest.skip("нет POSIX-совместимого sh (нужен dash или busybox)")

    root = SUITE.parents[1] / "files"
    # tune.sh included: it is what the measured 210 -> 359 Mbit/s hangs on, and a
    # bashism in it would only surface on the router, as a silent loss of half
    # the throughput.
    scripts = [
        root / "usr" / "lib" / "obfmesh" / "lib.sh",
        root / "usr" / "lib" / "obfmesh" / "apply.sh",
        root / "usr" / "lib" / "obfmesh" / "tune.sh",
        root / "usr" / "lib" / "obfmesh" / "watcher.sh",
        root / "usr" / "bin" / "obfmesh",
        root / "etc" / "init.d" / "obfmesh",
    ]
    for script in scripts:
        assert script.is_file(), script
        result = subprocess.run(
            [*shell, "-n", str(script)], capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, f"{script}: {result.stderr}"
