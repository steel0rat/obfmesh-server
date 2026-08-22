"""Идемпотентность reconcile() и поведение при смене числа лучей (SPEC, инварианты 2 и 3)."""

from __future__ import annotations

import pytest

from obfmesh import config_gen, orchestrator
from obfmesh.models import (
    SPOKE_MAX,
    Masking,
    ValidationError,
    client_address,
    wg_port,
)


def test_reconcile_is_idempotent(orch, database):
    first = orch.reconcile()
    assert first.errors == []
    assert first.changes, "первый проход обязан что-то создать"

    second = orch.reconcile()
    assert second.errors == []
    assert second.changes == [], f"повторный проход изменил состояние: {second.changes}"
    assert second.config_version == first.config_version


def test_reconcile_writes_obfuscator_configs(orch, database):
    orch.reconcile()
    settings = database.get_settings()
    for index in settings.spoke_indexes:
        path = orch.config_path(index)
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
        assert f"source-lport = {settings.obf_port(index)}" in content
        assert f"target = 127.0.0.1:{wg_port(index)}" in content
        # SPEC: маскировка STUN, регистр как у бинарника (README wg-obfuscator).
        assert "masking = STUN" in content
        import os

        assert os.stat(path).st_mode & 0o777 == 0o600


def test_shrinking_spokes_keeps_the_remaining_ones(orch, database):
    database.update_settings({"spokes": 4})
    orch.reconcile()
    before = {s.index: (s.public_key, s.listen_port) for s in database.list_spokes()}

    database.update_settings({"spokes": 2})
    report = orch.reconcile()
    assert report.errors == []

    after = {s.index: s for s in database.list_spokes()}
    for index in (1, 2):
        assert after[index].enabled is True
        assert (after[index].public_key, after[index].listen_port) == before[index], (
            "работающий луч перегенерировали"
        )
    for index in (3, 4):
        assert after[index].enabled is False
        # Ключи погашенного луча остаются: обратное включение не рвёт бандлы.
        assert after[index].public_key == before[index][0]


def test_growing_spokes_does_not_touch_existing(orch, database):
    orch.reconcile()
    before = {s.index: s.public_key for s in database.list_spokes()}
    database.update_settings({"spokes": 5})
    report = orch.reconcile()
    assert report.errors == []
    after = {s.index: s.public_key for s in database.list_spokes()}
    for index in before:
        assert after[index] == before[index]


def test_masking_none_is_reported_as_a_warning(orch, database):
    database.update_settings({"masking": Masking.NONE.value})
    report = orch.reconcile()
    assert any("masking=NONE" in item for item in report.warnings)
    assert report.ok, "предупреждение не должно превращаться в ошибку"


def test_peer_gets_only_its_own_address(orch, database):
    """У пира в allowed-ips ровно адрес своего луча — ни адреса чужого, ни общего.

    Общий адрес на все лучи и был причиной, по которой работал ровно один луч:
    сервер выбирал луч для ответа своим хешем, а сокет клиента, привязанный к
    другому лучу, эти пакеты отбрасывал.
    """
    database.create_client("router")
    database.update_settings({"spokes": 3})

    for index in (1, 2, 3):
        info = next(iter(database.client_peers(index).values()))
        assert orch._allowed_ips_for(info) == [f"{client_address(index, 0)}/32"]  # noqa: SLF001
        # 10.77.0.0/24 — сеть агрегатных адресов прежней схемы
        assert not any(item.startswith("10.77.0.") for item in orch._allowed_ips_for(info))  # noqa: SLF001


def test_reconcile_sets_the_peer_to_a_single_address(orch, database, monkeypatch):
    """Тот же инвариант, но на уровне команды `wg set`, а не расчёта списка."""
    from obfmesh import wg

    calls: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(
        wg, "set_peer", lambda name, key, allowed: calls.append((name, key, list(allowed)))
    )
    database.create_client("router")
    database.update_settings({"spokes": 2})

    orch.reconcile()

    assert calls, "пиры не выставлялись"
    for name, _, allowed in calls:
        index = int(name.removeprefix("swg"))
        assert allowed == [f"{client_address(index, 0)}/32"]


def test_input_rules_of_the_previous_port_base_are_removed(orch, database, monkeypatch):
    """Смена port_base оставляла в INPUT разрешения на порты, которые никто не слушает."""
    from obfmesh import wg

    monkeypatch.setenv("OBFMESH_MANAGE_INPUT", "1")
    deleted: list[tuple] = []
    monkeypatch.setattr(wg, "iptables_rule_exists", lambda *a, **k: True)
    monkeypatch.setattr(wg, "iptables_delete", lambda t, c, rule: deleted.append(rule))

    orch.reconcile()
    assert orchestrator._read_port_list(orch.input_ports_path()) == {48201, 48202}

    database.update_settings({"port_base": 49000})
    orch.reconcile()
    assert orchestrator._read_port_list(orch.input_ports_path()) == {49001, 49002}
    removed_ports = {rule[3] for rule in deleted}
    assert {"48201", "48202"} <= removed_ports


def test_port_base_colliding_with_wireguard_is_rejected(database):
    with pytest.raises(ValidationError, match="WireGuard"):
        database.update_settings({"port_base": 51820})
    # порт API тоже занимать нельзя
    with pytest.raises(ValidationError, match="API"):
        database.update_settings({"port_base": 8079})
    assert database.get_settings().port_base == 48200


def test_render_config_refuses_identical_ports(database):
    settings = database.get_settings()
    with pytest.raises(Exception, match="совпадают"):
        config_gen.render_obfuscator_config(1, 51821, 51821, settings)


def test_status_carries_counters_at_spoke_level(orch, database):
    orch.reconcile()
    payload = orch.status()
    assert payload["spokes"], "лучей нет"
    for entry in payload["spokes"]:
        # UI читает счётчики с уровня луча, а не из peers[]
        assert "rx_bytes" in entry and "tx_bytes" in entry
        assert "latest_handshake" in entry
        assert isinstance(entry["peers"], list)
    assert set(payload["totals"]) == {"rx_bytes", "tx_bytes", "latest_handshake"}


def test_status_does_not_mutate_pid_files(orch, database, monkeypatch, tmp_path):
    """GET /api/status не имеет права удалять файлы в /run."""
    monkeypatch.setenv("OBFMESH_DRY_RUN", "0")
    orch.reconcile()
    pid_path = orch.pid_path(1)
    with open(pid_path, "w", encoding="ascii") as handle:
        handle.write("999999\n")  # заведомо мёртвый pid
    orch.status()
    assert __import__("os").path.exists(pid_path), "status() удалил чужой pid-файл"


def test_spoke_indexes_cover_spec_range(database):
    settings = database.get_settings()
    settings.spokes = SPOKE_MAX
    assert settings.spoke_indexes == list(range(1, SPOKE_MAX + 1))
    assert settings.obf_port(1) == 48201
    assert wg_port(1) == 51821


def test_reconcile_survives_a_broken_spoke(orch, database, monkeypatch):
    """Сбой одного луча не должен ронять весь проход, включая пролог и эпилог."""
    original = orch._reconcile_spoke

    def explode(spoke, settings, report):
        if spoke.index == 1:
            raise OSError("ip link add: устройство недоступно")
        return original(spoke, settings, report)

    monkeypatch.setattr(orch, "_reconcile_spoke", explode)
    report = orch.reconcile()
    assert any("луч 1" in item for item in report.errors)
    assert report.config_version > 0, "эпилог не выполнился"
    assert 2 in report.spokes


def test_reconcile_survives_a_database_hiccup(orch, database, monkeypatch):
    """sqlite3.OperationalError не наследуется от ObfmeshError и раньше ронял проход."""
    import sqlite3

    def explode(indexes=None):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(database, "ensure_client_keys", explode)
    report = orch.reconcile()
    assert any("ключи клиентов" in item for item in report.errors)
    # лучи всё равно сверены
    assert report.spokes == database.get_settings().spoke_indexes
    assert orchestrator.RECOVERABLE  # тип перехвата на месте
