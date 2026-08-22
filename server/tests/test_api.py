"""HTTP-слой: коды ответов, аутентификация и отсутствие секретов в телах."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from obfmesh import app as app_module

ADMIN_KEY = "test-admin-key-0123456789"


@pytest.fixture
def client(env, monkeypatch):
    monkeypatch.setenv("OBFMESH_ADMIN_KEY", ADMIN_KEY)
    application = app_module.create_app()
    with TestClient(application) as test_client:
        yield test_client


def admin(headers=None):
    data = {"X-API-Key": ADMIN_KEY}
    if headers:
        data.update(headers)
    return data


def test_anonymous_request_is_401(client):
    response = client.get("/api/status")
    assert response.status_code == 401


def test_client_token_on_admin_endpoint_is_403(client):
    created = client.post("/api/clients", json={"name": "router"}, headers=admin())
    token = created.json()["token"]
    response = client.get("/api/status", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_bad_admin_key_is_401(client):
    response = client.get("/api/status", headers={"X-API-Key": "wrong-but-long-enough-key"})
    assert response.status_code == 401


def test_duplicate_client_is_409(client):
    assert client.post("/api/clients", json={"name": "router"}, headers=admin()).status_code == 201
    response = client.post("/api/clients", json={"name": "router"}, headers=admin())
    assert response.status_code == 409


def test_missing_client_is_404(client):
    assert client.delete("/api/clients/ghost", headers=admin()).status_code == 404
    assert client.get("/api/clients/ghost/bundle", headers=admin()).status_code == 404


def test_settings_roundtrip_and_idempotency(client):
    first = client.patch("/api/settings", json={"spokes": 3}, headers=admin())
    assert first.status_code == 200
    assert first.json()["spokes"] == 3

    second = client.patch("/api/settings", json={"spokes": 3}, headers=admin())
    assert second.status_code == 200
    # Ничего не изменилось — версия не растёт и reconcile не запускается.
    assert "reconcile" not in second.json()


def test_agg_mode_is_no_longer_a_setting(client):
    """Режимы агрегации убраны: их нет в ответе и попытка задать — честная 422.

    Молча проглотить agg_mode нельзя: сервер его больше не исполняет, и вызов,
    считающий иначе, обязан узнать об этом сразу.
    """
    assert "agg_mode" not in client.get("/api/settings", headers=admin()).json()

    for mode in ("single", "ecmp", "teql"):
        response = client.patch("/api/settings", json={"agg_mode": mode}, headers=admin())
        assert response.status_code == 422, mode


def test_port_base_colliding_with_wireguard_is_refused(client):
    response = client.patch("/api/settings", json={"port_base": 51820}, headers=admin())
    assert response.status_code == 422
    assert client.get("/api/settings", headers=admin()).json()["port_base"] == 48200


def test_masking_none_comes_back_with_a_warning(client):
    response = client.patch("/api/settings", json={"masking": "NONE"}, headers=admin())
    assert response.status_code == 200
    warnings = response.json()["reconcile"]["warnings"]
    assert any("masking=NONE" in item for item in warnings)


def test_clients_listing_carries_no_token_material(client):
    created = client.post("/api/clients", json={"name": "router"}, headers=admin())
    token = created.json()["token"]

    listing = client.get("/api/clients", headers=admin()).json()
    assert listing[0]["name"] == "router"
    assert "token_prefix" not in listing[0]
    # token_id — префикс sha256, не часть токена
    assert not token.startswith(listing[0]["token_id"])
    assert len(listing[0]["token_id"]) == 8


def test_status_hides_private_keys(client):
    client.post("/api/clients", json={"name": "router"}, headers=admin())
    payload = client.get("/api/status", headers=admin()).json()
    for spoke in payload["spokes"]:
        assert spoke["private_key"] == "<есть>"
    assert payload["settings"]["key"] == "<есть>"


def test_own_bundle_needs_a_bearer_token(client):
    created = client.post("/api/clients", json={"name": "router"}, headers=admin())
    token = created.json()["token"]

    assert client.get("/api/bundle").status_code == 401
    response = client.get("/api/bundle", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["spokes"][0]["local_port"] == 13301

    etag = response.headers["ETag"]
    cached = client.get(
        "/api/bundle",
        headers={"Authorization": f"Bearer {token}", "If-None-Match": etag},
    )
    assert cached.status_code == 304


def _request(headers: dict[str, str]):
    """Минимальный Request для прямого вызова зависимости.

    Сам поток /api/events бесконечен, и TestClient на нём не закрывается, поэтому
    проверяется именно правило аутентификации, а не транспорт.
    """
    from fastapi import Request

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 80),
            "client": ("10.0.0.1", 1234),
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        }
    )


def test_events_falls_back_to_the_bearer_token(client):
    """Роутер шлёт оба заголовка; протухший admin-ключ не должен рвать SSE."""
    from obfmesh import auth

    created = client.post("/api/clients", json={"name": "router"}, headers=admin())
    token = created.json()["token"]

    principal = auth.require_admin_or_client(
        _request({"X-API-Key": "stale-admin-key-but-long", "Authorization": f"Bearer {token}"})
    )
    assert principal.kind == "client"
    assert principal.name == "router"

    principal = auth.require_admin_or_client(_request({"X-API-Key": ADMIN_KEY}))
    assert principal.kind == "admin"


def test_events_rejects_both_credentials_wrong(client):
    headers = {"X-API-Key": "stale-admin-key-but-long", "Authorization": "Bearer not-a-real-token"}
    response = client.get("/api/events", headers=headers)
    assert response.status_code == 401
