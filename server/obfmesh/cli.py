"""Локальное управление сервером без HTTP: `python -m obfmesh.cli <команда>`.

Нужна там, где API недоступен или не должен участвовать:

    teardown    погасить обфускаторы по pid-файлам (после `systemctl stop`)
    reconcile   привести систему к состоянию из базы
    status      короткая сводка по лучам

Значения ключей не печатаются: сводка показывает только факт их наличия.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import __version__, orchestrator
from .models import MASKED


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    from . import auth

    auth.install_log_redaction()


def _cmd_teardown(args: argparse.Namespace) -> int:
    orch = orchestrator.get_orchestrator()
    stopped = orch.teardown_processes()
    if not stopped:
        print("работающих обфускаторов не найдено")
        return 0
    for line in stopped:
        print(line)
    if args.interfaces:
        from . import wg
        from .models import SPOKE_MAX, iface_name

        for index in range(1, SPOKE_MAX + 1):
            name = iface_name(index)
            if wg.interface_exists(name):
                wg.delete_interface(name)
                print(f"{name}: интерфейс удалён")
    return 0


def _cmd_reconcile(args: argparse.Namespace) -> int:
    report = orchestrator.get_orchestrator().reconcile()
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for line in report.changes:
            print(f"изменено: {line}")
        for line in report.actions:
            print(f"действие: {line}")
        for line in report.warnings:
            print(f"внимание: {line}")
        for line in report.errors:
            print(f"ОШИБКА:   {line}")
        print(f"итог: {'ok' if report.ok else 'с ошибками'}, config_version={report.config_version}")
    return 0 if report.ok else 1


def _cmd_status(args: argparse.Namespace) -> int:
    payload = orchestrator.get_orchestrator().status()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    settings = payload["settings"]
    print(f"obfmesh {payload['version']}, config_version={payload['config_version']}")
    print(
        f"лучей {settings['spokes']}, masking={settings['masking']}, "
        f"mtu={settings['mtu']}, ключ обфускации {settings['key'] or MASKED}"
    )
    for spoke in payload["spokes"]:
        obf = spoke["obfuscator"]
        print(
            f"  луч {spoke['index']:>2} {spoke['iface']:<6} "
            f"link={'up' if spoke['iface_up'] else 'down':<4} "
            f"обфускатор={'pid ' + str(obf['pid']) if obf['running'] else 'НЕ РАБОТАЕТ':<12} "
            f":{spoke['obf_port']} -> 127.0.0.1:{spoke['listen_port']} "
            f"rx={spoke['rx_bytes']} tx={spoke['tx_bytes']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="obfmesh-ctl", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="подробный лог")
    parser.add_argument("--version", action="version", version=f"obfmesh {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    teardown = sub.add_parser("teardown", help="остановить обфускаторы по pid-файлам")
    teardown.add_argument(
        "--interfaces", action="store_true", help="дополнительно удалить интерфейсы swg{i}"
    )
    teardown.set_defaults(func=_cmd_teardown)

    reconcile = sub.add_parser("reconcile", help="привести систему к состоянию из базы")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=_cmd_reconcile)

    status = sub.add_parser("status", help="сводка по лучам")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    args = parser.parse_args(argv)
    os.umask(0o077)
    _setup_logging(args.verbose)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
