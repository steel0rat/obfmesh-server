"""Обратного пути к клиенту сервер больше не строит — проверка, что это так.

Прежняя схема давала клиенту общий агрегатный адрес 10.77.0.{4k+2} и ставила к
нему многопутевой маршрут с nexthop через каждый поднятый swg{i}. На живых
машинах она убивала трафик: сокет клиента привязан к лучу и отбрасывает пакеты,
пришедшие на другой, а сервер выбирал луч для ответа своим хешем — работал ровно
один луч, и какой именно, менялось от замера к замеру.

Работающая схема симметрична: у пира ровно один адрес 10.77.{i}.2/32, ответ
уходит через тот же swg{i}, откуда пришёл запрос, по connected-маршруту. Ставить
для этого нечего.

Здесь подменяется не отдельная функция wg.py, а сам `wg.run()`: фейк держит
таблицу маршрутов в памяти и печатает её ровно так, как `ip -o route show`.
Поэтому под проверкой и сборка argv, и разбор вывода, и логика оркестратора.
"""

from __future__ import annotations

import ipaddress
import json
import os

import pytest

from obfmesh import orchestrator, wg
from obfmesh.models import SPOKE_MAX

AGG = "10.77.0.2/32"  # агрегатный адрес клиента в слоте 0 из прежней схемы
AGG_SECOND = "10.77.0.6/32"  # слот 1
LEGACY_AGG_NET = "10.77.0.0/24"


class FakeIp:
    """Подмена wg.run(): таблица маршрутов и состояние линков в памяти."""

    def __init__(self, links_up: list[str]) -> None:
        self.routes: dict[str, list[str]] = {}
        self.up = set(links_up)
        self.calls: list[list[str]] = []

    # --- наблюдение за вызовами -------------------------------------------

    def commands(self, *prefix: str) -> list[list[str]]:
        """Вызовы `ip`, начинающиеся с указанных слов (без пути к бинарнику)."""
        return [c[1:] for c in self.calls if tuple(c[1 : 1 + len(prefix)]) == prefix]

    def reset_calls(self) -> None:
        self.calls.clear()

    # --- сам обработчик ----------------------------------------------------

    def __call__(
        self,
        argv,
        *,
        check: bool = True,
        timeout: float = 15.0,
        dry_returncode: int = 0,
        dry_stdout: str = "",
    ) -> wg.CommandResult:
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        result = self._dispatch(argv)
        if result is None:
            # Всё, чего фейк не знает, отвечает как проба в dry-run.
            return wg.CommandResult(argv, dry_returncode, dry_stdout, "", executed=False)
        if check and not result.ok:
            raise wg.CommandError(argv, result.returncode, result.stderr)
        return result

    def _dispatch(self, argv: list[str]) -> wg.CommandResult | None:
        if os.path.basename(argv[0]) != "ip":
            return None
        rest = argv[1:]

        if rest[:1] == ["-j"] and rest[1:4] == ["link", "show", "dev"]:
            device = rest[4]
            if device not in self.up:
                return wg.CommandResult(argv, 1, "", "Device does not exist")
            payload = [{"ifname": device, "mtu": 1400, "flags": ["POINTOPOINT", "UP", "LOWER_UP"]}]
            return wg.CommandResult(argv, 0, json.dumps(payload))

        if rest[:1] == ["-o"] and rest[1:4] == ["route", "show", "to"]:
            if rest[4] == "exact":
                cidr = _norm(rest[5])
                if cidr not in self.routes:
                    return wg.CommandResult(argv, 0, "")
                return wg.CommandResult(argv, 0, self._render(cidr) + "\n")
            if rest[4] == "root":
                root = ipaddress.ip_network(rest[5])
                lines = [
                    self._render(cidr)
                    for cidr in sorted(self.routes)
                    if ipaddress.ip_network(cidr).subnet_of(root)
                ]
                return wg.CommandResult(argv, 0, "".join(f"{line}\n" for line in lines))
            return None

        if rest[:2] == ["route", "replace"]:
            cidr = _norm(rest[2])
            devices = [rest[i + 1] for i, token in enumerate(rest) if token == "dev"]
            self.routes[cidr] = devices
            return wg.CommandResult(argv, 0)

        if rest[:2] == ["route", "del"]:
            cidr = _norm(rest[2])
            if self.routes.pop(cidr, None) is None:
                return wg.CommandResult(argv, 2, "", "RTNETLINK answers: No such process")
            return wg.CommandResult(argv, 0)

        return None

    def _render(self, cidr: str) -> str:
        """Строка ровно того вида, что печатает `ip -o route show`."""
        # host-маршрут iproute2 показывает без /32
        shown = cidr[: -len("/32")] if cidr.endswith("/32") else cidr
        devices = self.routes[cidr]
        if not devices:
            # Маршрут, в который нечего отправить: префикс и больше ничего.
            return f"{shown} "
        if len(devices) == 1:
            return f"{shown} dev {devices[0]} scope link "
        legs = " ".join(f"\\\tnexthop dev {device} weight 1" for device in devices)
        return f"{shown} {legs} "


def _norm(value: str) -> str:
    return str(ipaddress.ip_network(value, strict=False))


@pytest.fixture
def routed(orch, database, monkeypatch):
    """Оркестратор поверх фейкового `ip`, все лучи подняты, один клиент заведён.

    dry-run выключен: проход должен пройти через настоящую сборку команд и
    настоящий разбор вывода `ip`. Обфускаторы и ip_forward заглушены — им нужен
    бинарник и /proc, а речь здесь о маршрутах.
    """
    monkeypatch.setenv("OBFMESH_DRY_RUN", "0")
    fake = FakeIp(links_up=[f"swg{i}" for i in range(1, SPOKE_MAX + 1)])
    monkeypatch.setattr(wg, "run", fake)
    monkeypatch.setattr(wg, "ip_forward_enabled", lambda: True)
    monkeypatch.setattr(orch, "_reconcile_obfuscator", lambda spoke, settings, report: None)
    database.create_client("router")
    return orch, database, fake


def sweep(orch) -> orchestrator.ReconcileReport:
    """Один проход только по уборке маршрутов прежней схемы."""
    report = orchestrator.ReconcileReport()
    orch._sweep_legacy_aggregate_routes(report)  # noqa: SLF001
    return report


# --- маршрут не ставится ----------------------------------------------------


@pytest.mark.parametrize("spokes", list(range(1, SPOKE_MAX + 1)))
def test_reconcile_installs_no_route_for_any_spoke_count(routed, spokes):
    """Ни при каком числе лучей обратный маршрут не появляется."""
    orch, database, fake = routed
    database.update_settings({"spokes": spokes})

    report = orch.reconcile()

    assert report.errors == []
    assert fake.routes == {}
    assert fake.commands("route", "replace") == []


def test_no_command_ever_carries_a_nexthop(routed):
    """Многопутевой маршрут собирается из nexthop — этого слова быть не должно."""
    orch, database, fake = routed
    database.create_client("second")
    database.update_settings({"spokes": 3})

    orch.reconcile()

    assert not [argv for argv in fake.calls if "nexthop" in argv]


def test_several_clients_get_no_route_either(routed):
    """Маршрут ставился на каждого клиента отдельно: клиентов больше — нулей больше."""
    orch, database, fake = routed
    for name in ("second", "third"):
        database.create_client(name)

    orch.reconcile()

    assert fake.routes == {}
    assert wg.route_table(LEGACY_AGG_NET) == {}


def test_multipath_helpers_are_gone():
    """Прямой сторож против возврата схемы: помощников для неё в wg.py нет.

    `replace_multipath_route()` собирала маршрут с nexthop на каждый луч,
    `set_rp_filter_loose()` ослабляла обратную проверку пути, потому что при
    таком маршруте путь был асимметричным. Симметричной схеме не нужно ни то,
    ни другое.
    """
    assert not hasattr(wg, "replace_multipath_route")
    assert not hasattr(wg, "set_rp_filter_loose")


# --- уборка за прежней схемой ------------------------------------------------


def test_legacy_multipath_route_is_swept(routed):
    """Маршрут пережил обновление в /run и в ядре — проход обязан его снять."""
    orch, _, fake = routed
    fake.routes[AGG] = ["swg1", "swg2"]
    fake.routes[AGG_SECOND] = ["swg1", "swg2"]
    with open(orch.legacy_routes_path(), "w", encoding="ascii") as handle:
        handle.write(f"{AGG}\n{AGG_SECOND}\n")

    report = sweep(orch)

    assert fake.routes == {}
    assert sorted(fake.commands("route", "del")) == [
        ["route", "del", AGG],
        ["route", "del", AGG_SECOND],
    ]
    assert any(AGG in item for item in report.actions)
    assert not os.path.exists(orch.legacy_routes_path())


def test_legacy_journal_entry_outside_the_aggregate_network_is_swept(routed):
    """До агрегатного адреса маршрут ставили на /32 из подсети первого луча."""
    orch, _, fake = routed
    fake.routes["10.77.1.2/32"] = ["swg1", "swg2"]
    with open(orch.legacy_routes_path(), "w", encoding="ascii") as handle:
        handle.write("10.77.1.2/32\n")

    sweep(orch)

    assert fake.routes == {}


def test_orphan_route_is_swept_without_a_journal(routed):
    """Журнал живёт в /run: без него уборку делает обход сети агрегатов."""
    orch, _, fake = routed
    fake.routes["10.77.0.99/32"] = ["swg1"]
    assert not os.path.exists(orch.legacy_routes_path())

    sweep(orch)

    assert fake.routes == {}


def test_sweep_is_idempotent(routed):
    """Убирать больше нечего — второй проход молчит и ничего не вызывает."""
    orch, _, fake = routed
    fake.routes[AGG] = ["swg1", "swg2"]
    sweep(orch)
    fake.reset_calls()

    report = sweep(orch)

    assert report.actions == []
    assert report.changes == []
    assert fake.commands("route", "del") == []


def test_sweep_leaves_spoke_networks_alone(routed):
    """Уборка ходит только по 10.77.0.0/24: подсети лучей — не её дело."""
    orch, _, fake = routed
    fake.routes["10.77.1.0/24"] = ["swg1"]
    fake.routes["10.77.2.0/24"] = ["swg2"]

    sweep(orch)

    assert fake.routes == {"10.77.1.0/24": ["swg1"], "10.77.2.0/24": ["swg2"]}


def test_full_reconcile_sweeps_and_then_stays_quiet(routed):
    """Уборка встроена в общий проход и не мешает его идемпотентности."""
    orch, _, fake = routed
    fake.routes[AGG] = ["swg1", "swg2"]

    first = orch.reconcile()
    assert first.errors == []
    assert any("прежней схемы" in item for item in first.actions)

    second = orch.reconcile()
    assert second.errors == []
    assert not any("прежней схемы" in item for item in second.actions)
    assert second.changes == []


# --- разбор вывода ip -------------------------------------------------------


def test_route_devices_reads_a_multipath_route(routed):
    """Разбор многопутевого вывода нужен, чтобы такие маршруты находить и сносить."""
    _, _, fake = routed
    fake.routes[AGG] = ["swg1", "swg2", "swg3"]
    assert wg.route_devices(AGG) == ["swg1", "swg2", "swg3"]


def test_route_devices_reads_a_single_device_route(routed):
    _, _, fake = routed
    fake.routes[AGG] = ["swg1"]
    assert wg.route_devices(AGG) == ["swg1"]


def test_route_devices_of_a_route_without_devices_is_empty(routed):
    _, _, fake = routed
    fake.routes[AGG] = []
    assert wg.route_devices(AGG) == []


def test_route_table_covers_only_the_aggregate_network(routed):
    _, _, fake = routed
    fake.routes["10.77.0.6/32"] = ["swg1", "swg2"]
    fake.routes["10.77.1.0/24"] = ["swg1"]
    assert wg.route_table(LEGACY_AGG_NET) == {"10.77.0.6/32": ["swg1", "swg2"]}


def test_route_table_sees_a_route_without_devices(routed):
    """Битый маршрут обязан попасть в снимок — иначе его некому убрать."""
    _, _, fake = routed
    fake.routes["10.77.0.6/32"] = []
    assert wg.route_table(LEGACY_AGG_NET) == {"10.77.0.6/32": []}
