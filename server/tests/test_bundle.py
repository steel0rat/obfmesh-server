"""Бандл обязан совпадать с разделом «Формат бандла» SPEC.md буква в букву."""

from __future__ import annotations

import pytest

from obfmesh import config_gen
from obfmesh.config_gen import BundleError
from obfmesh.models import (
    CLIENT_PORT_BASE,
    SERVER_PORT_BASE,
    WG_PORT_BASE,
)

# Ровно то, что перечислено в SPEC.md, раздел «Формат бандла». Ни agg_mode, ни
# agg_address: режимов агрегации не существует, общего адреса не существует.
TOP_LEVEL_KEYS = {"config_version", "server", "obfuscation_key", "spokes"}
SERVER_KEYS = {"host", "masking", "mtu"}
SPOKE_KEYS = {
    "index",
    "server_port",
    "local_port",
    "wg_private_key",
    "wg_server_pubkey",
    "address",
    "peer_address",
}


@pytest.fixture
def ready(orch, database):
    database.create_client("router")
    orch.reconcile()
    return database


def test_bundle_shape_matches_spec(ready):
    bundle = config_gen.bundle_dict("router", database=ready)
    assert set(bundle) == TOP_LEVEL_KEYS
    assert set(bundle["server"]) == SERVER_KEYS
    assert bundle["server"]["host"] == "45.136.127.10"
    assert bundle["server"]["masking"] == "STUN"
    assert bundle["server"]["mtu"] == 1400
    for spoke in bundle["spokes"]:
        assert set(spoke) == SPOKE_KEYS


def test_bundle_ports_follow_spec_invariant_6(ready):
    bundle = config_gen.bundle_dict("router", database=ready)
    for spoke in bundle["spokes"]:
        index = spoke["index"]
        assert spoke["server_port"] == SERVER_PORT_BASE + index
        assert spoke["local_port"] == CLIENT_PORT_BASE + index
        # внутренний порт WireGuard в бандл не входит, но формула та же
        assert WG_PORT_BASE + index == 51820 + index


def test_bundle_addresses_follow_spec_topology(ready):
    bundle = config_gen.bundle_dict("router", database=ready)
    for spoke in bundle["spokes"]:
        index = spoke["index"]
        assert spoke["peer_address"] == f"10.77.{index}.1"
        # первый клиент (слот 0) — ровно пример из SPEC
        assert spoke["address"] == f"10.77.{index}.2/30"


def test_bundle_carries_no_aggregate_address(ready):
    """Общего адреса на все лучи нет нигде: ни отдельным полем, ни в лучах.

    10.77.0.0/24 — сеть агрегатных адресов прежней схемы. Ни один адрес бандла
    не имеет права туда попасть: сервер такой адрес больше не пускает в
    allowed-ips и обратного маршрута к нему не ставит.
    """
    bundle = config_gen.bundle_dict("router", database=ready)
    assert "agg_address" not in bundle
    for spoke in bundle["spokes"]:
        assert not spoke["address"].startswith("10.77.0.")
        assert not spoke["peer_address"].startswith("10.77.0.")


def test_bundle_carries_no_aggregation_mode(ready):
    """SPEC.md:141 — поля agg_mode в бандле нет ни при какой настройке.

    Пока оно там было, клиент 1.1.0 бандл принимал, перестраивался из ecmp/teql
    в single и молча схлопывался на один луч. Порядок выката (клиент → сервер)
    построен на обратном: клиент 1.1.0 обязан бандл отвергнуть и остаться на
    прежней конфигурации.
    """
    assert "agg_mode" not in config_gen.bundle_dict("router", database=ready)

    ready.update_settings({"spokes": 3, "mtu": 1380})
    assert "agg_mode" not in config_gen.bundle_dict("router", database=ready)


def test_second_client_gets_its_own_slot(ready):
    ready.create_client("second")
    ready.ensure_client_keys(ready.get_settings().spoke_indexes)
    bundle = config_gen.bundle_dict("second", database=ready)
    for spoke in bundle["spokes"]:
        assert spoke["address"] == f"10.77.{spoke['index']}.6/30"


def test_bundle_refuses_a_spoke_without_keys(ready):
    """SPEC, инвариант 5: луч без ключей в бандл не попадает."""
    client = ready.get_client("router")
    with ready._tx() as conn:  # noqa: SLF001 - тест лезет в базу намеренно
        conn.execute("DELETE FROM client_keys WHERE client_id = ? AND spoke_idx = 1", (client.id,))
    with pytest.raises(BundleError, match="нет ключей"):
        config_gen.bundle_dict("router", database=ready)


def test_bundle_etag_is_stable(ready):
    bundle = config_gen.bundle_dict("router", database=ready)
    assert config_gen.bundle_etag(bundle) == config_gen.bundle_etag(dict(bundle))


def test_safe_dict_hides_key_material(ready):
    bundle = config_gen.build_bundle("router", database=ready)
    safe = bundle.safe_dict()
    assert safe["obfuscation_key"] == "<есть>"
    for spoke in safe["spokes"]:
        assert spoke["wg_private_key"] == "<есть>"
