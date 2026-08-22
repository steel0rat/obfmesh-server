"""Сборка клиентского бандла.

Формат зафиксирован SPEC.md, раздел «Формат бандла»: имена и смысл описанных там
полей здесь не меняются — клиентский apply.sh разбирает их через jsonfilter по
фиксированным путям. Новые поля добавлять можно, старый клиент их не заметит.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

from .db import Database, get_db
from .models import (
    Bundle,
    BundleSpoke,
    Client,
    ObfmeshError,
    Settings,
    client_local_port,
    server_address,
    server_obf_port,
)

log = logging.getLogger("obfmesh.config_gen")

DEFAULT_OBF_VERBOSE = 1


class BundleError(ObfmeshError):
    """Бандл собрать нельзя: нет клиента, ключей или внешнего адреса."""


def build_bundle(client: Client | str, *, database: Database | None = None) -> Bundle:
    """Собрать бандл клиента.

    В бандл попадают все включённые лучи 1..spokes.

    Each entry describes one self-contained spoke: `address` is the only address
    its peer accepts, so the client keeps it on owg{i} with a routing table of
    its own. Which spoke a service uses is decided on the client by binding to
    that interface - the server publishes no shared address to spread traffic
    over.
    """
    db = database or get_db()
    settings = db.get_settings()

    if isinstance(client, str):
        found = db.get_client(client)
        if found is None:
            raise BundleError(f"клиент {client!r} не найден")
        client = found

    if not settings.external_host:
        raise BundleError(
            "external_host не задан: укажите адрес сервера через PATCH /api/settings "
            "или переменную OBFMESH_EXTERNAL_HOST при установке"
        )

    spokes: list[BundleSpoke] = []
    missing: list[int] = []
    for spoke in db.list_spokes(include_disabled=False):
        if spoke.index > settings.spokes:
            continue
        key = client.keys.get(spoke.index)
        if key is None or not key.private_key:
            missing.append(spoke.index)
            continue
        spokes.append(
            BundleSpoke(
                index=spoke.index,
                server_port=server_obf_port(spoke.index, settings.port_base),
                local_port=client_local_port(spoke.index),
                wg_private_key=key.private_key,
                wg_server_pubkey=spoke.public_key,
                address=key.address_cidr,
                peer_address=server_address(spoke.index),
            )
        )

    if missing:
        # Клиент не должен получить луч без ключей (SPEC, инвариант 5).
        raise BundleError(
            f"у клиента {client.name!r} нет ключей для лучей {missing}; "
            "выполните reconcile() — он догенерирует недостающие"
        )
    if not spokes:
        raise BundleError("нет ни одного включённого луча: сначала выполните reconcile()")

    return Bundle(
        config_version=settings.config_version,
        host=settings.external_host,
        masking=settings.masking,
        mtu=settings.mtu,
        obfuscation_key=settings.obfuscation_key,
        spokes=spokes,
    )


def bundle_dict(client: Client | str, *, database: Database | None = None) -> dict:
    return build_bundle(client, database=database).to_dict()


def bundle_json(client: Client | str, *, database: Database | None = None, indent: int | None = None) -> str:
    return json.dumps(bundle_dict(client, database=database), ensure_ascii=False, indent=indent, sort_keys=False)


def bundle_etag(bundle: Bundle | dict) -> str:
    """Стабильный хеш бандла — для ETag и сравнения «изменилось ли» на клиенте."""
    data = bundle.to_dict() if isinstance(bundle, Bundle) else bundle
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_obfuscator_config(index: int, obf_port: int, wg_port: int, settings: Settings) -> str:
    """Конфиг wg-obfuscator v1.6 для одного луча.

    Имена ключей и регистр значений — из README wg-obfuscator: source-if,
    source-lport, target, key, masking, verbose; masking принимает NONE, AUTO,
    STUN заглавными. Клиентская половина проекта пишет такой же файл теми же
    ключами и запускает `wg-obfuscator -c <файл>` — одно описание бинарника
    на оба конца. Секция даёт имя инстанса в логах обфускатора.
    """
    if obf_port == wg_port:
        # Иначе обфускатор слушал бы порт, который уже занял WireGuard луча,
        # и форвардил трафик сам себе. Settings.validate() ловит это раньше;
        # проверка здесь закрывает путь мимо валидации.
        raise ObfmeshError(
            f"луч {index}: порт обфускатора и порт WireGuard совпадают ({obf_port})"
        )
    return (
        f"# obfmesh: луч {index}. Файл генерируется автоматически, правки будут перезаписаны.\n"
        f"[spoke{index}]\n"
        f"source-if = 0.0.0.0\n"
        f"source-lport = {obf_port}\n"
        f"target = 127.0.0.1:{wg_port}\n"
        f"key = {settings.obfuscation_key}\n"
        f"masking = {settings.masking.value}\n"
        f"verbose = {obfuscator_verbose()}\n"
    )


def obfuscator_verbose() -> int:
    """Уровень логирования обфускатора, 0..4. OBFMESH_OBF_VERBOSE переопределяет.

    По умолчанию 1 (предупреждения): клиент за CGNAT постоянно меняет исходный
    порт, и на уровне INFO обфускатор пишет строку на каждую такую смену.
    """
    raw = os.environ.get("OBFMESH_OBF_VERBOSE", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_OBF_VERBOSE
    return value if 0 <= value <= 4 else DEFAULT_OBF_VERBOSE
