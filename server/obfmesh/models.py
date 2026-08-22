"""Модели данных obfmesh и сетевая арифметика лучей.

Числовые константы зафиксированы SPEC.md (раздел «Инварианты», п. 6) и не настраиваются,
кроме `port_base` серверного обфускатора.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# --- Константы, зафиксированные SPEC ---------------------------------------

SPOKE_MIN = 1
SPOKE_MAX = 10

WG_PORT_BASE = 51820  # внутренний WireGuard луча i: 51820 + i
CLIENT_PORT_BASE = 13300  # клиентский обфускатор луча i: 13300 + i
SERVER_PORT_BASE = 48200  # серверный обфускатор луча i: port_base + i

# Порт uvicorn из obfmesh-server.service. Обфускатору его отдавать нельзя:
# reconcile поднимает лучи, когда API уже слушает, и bind просто не удастся.
DEFAULT_API_PORT = 8080

IFACE_PREFIX = "swg"
MESH_NET = ipaddress.IPv4Network("10.77.0.0/16")

MTU_MIN = 1280
MTU_MAX = 1500
MTU_DEFAULT = 1400

# Каждому клиенту выдаётся /30-срез внутри 10.77.{i}.0/24: слот k → 10.77.{i}.{4k}/30,
# сервер .1, клиент .{4k+2}. Слот 0 даёт ровно 10.77.{i}.2/30 из примера бандла в SPEC.
CLIENT_SLOT_STRIDE = 4
CLIENT_SLOT_MAX = 63

# Value the retired `agg_mode` column of the settings table is frozen at. It is a
# database artefact and nothing else: aggregation modes are gone, the bundle does
# not carry the field (SPEC, "Формат бандла") and the API does not accept it. The
# column is NOT NULL without a default, so dropping it would rebuild the table;
# freezing it on the one value a rolled-back 1.1.x server can still parse costs
# nothing. See db._migration_003().
FROZEN_AGG_MODE_COLUMN = "single"

# Строка-заглушка для отчётов и логов вместо значения ключа (SPEC, инвариант 1).
MASKED = "<есть>"

CLIENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Ключ уходит в конфиг wg-obfuscator как значение `key = ...`, поэтому запрещены
# символы, ломающие его парсер (#, ;, =, пробелы, переводы строк).
OBFUSCATION_KEY_RE = re.compile(r"^[A-Za-z0-9_\-.@+:/]{8,255}$")
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
WG_KEY_RE = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")


class ObfmeshError(Exception):
    """Базовая ошибка obfmesh."""


class ValidationError(ObfmeshError):
    """Некорректные входные данные."""


class Masking(str, Enum):
    NONE = "NONE"
    AUTO = "AUTO"
    STUN = "STUN"


# --- Сетевая арифметика -----------------------------------------------------


def validate_spoke_index(index: int) -> int:
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValidationError(f"индекс луча должен быть целым числом, получено {index!r}")
    if not SPOKE_MIN <= index <= SPOKE_MAX:
        raise ValidationError(f"индекс луча вне диапазона {SPOKE_MIN}..{SPOKE_MAX}: {index}")
    return index


def iface_name(index: int) -> str:
    return f"{IFACE_PREFIX}{validate_spoke_index(index)}"


def wg_port(index: int) -> int:
    return WG_PORT_BASE + validate_spoke_index(index)


def client_local_port(index: int) -> int:
    return CLIENT_PORT_BASE + validate_spoke_index(index)


def server_obf_port(index: int, port_base: int = SERVER_PORT_BASE) -> int:
    return port_base + validate_spoke_index(index)


def api_port() -> int:
    """Порт управляющего API. OBFMESH_API_PORT держит его в курсе правок юнита."""
    raw = os.environ.get("OBFMESH_API_PORT", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_API_PORT
    return value if 1 <= value <= 65535 else DEFAULT_API_PORT


def spoke_network(index: int) -> ipaddress.IPv4Network:
    return ipaddress.IPv4Network(f"10.77.{validate_spoke_index(index)}.0/24")


def server_address(index: int) -> str:
    return f"10.77.{validate_spoke_index(index)}.1"


def server_address_cidr(index: int) -> str:
    """Адрес сервера на swg{i}. Маска /24: на интерфейсе живут все /30-срезы клиентов."""
    return f"{server_address(index)}/24"


def validate_client_slot(slot: int) -> int:
    if not isinstance(slot, int) or isinstance(slot, bool):
        raise ValidationError(f"слот клиента должен быть целым числом, получено {slot!r}")
    if not 0 <= slot <= CLIENT_SLOT_MAX:
        raise ValidationError(f"слот клиента вне диапазона 0..{CLIENT_SLOT_MAX}: {slot}")
    return slot


def client_address(index: int, slot: int) -> str:
    host = CLIENT_SLOT_STRIDE * validate_client_slot(slot) + 2
    return f"10.77.{validate_spoke_index(index)}.{host}"


def client_address_cidr(index: int, slot: int) -> str:
    return f"{client_address(index, slot)}/30"


def client_allowed_ips(index: int, slot: int) -> str:
    """Адрес клиента на конкретном луче в виде AllowedIPs пира."""
    return f"{client_address(index, slot)}/32"


def validate_wg_key(value: str, what: str = "ключ") -> str:
    if not isinstance(value, str) or not WG_KEY_RE.match(value):
        raise ValidationError(f"{what}: некорректный формат WireGuard-ключа")
    return value


# --- Модели -----------------------------------------------------------------


@dataclass
class Settings:
    """Глобальные настройки. Ровно одна строка в БД."""

    # Two spokes is the measured optimum on a 4x Cortex-A55 router: three gave
    # 210-222 Mbit/s against 359-373 on two, because every extra spoke is one
    # more obfuscator process competing for the same cores.
    spokes: int = 2
    masking: Masking = Masking.STUN
    mtu: int = MTU_DEFAULT
    external_host: str = ""
    port_base: int = SERVER_PORT_BASE
    obfuscation_key: str = field(default="", repr=False)
    obfuscator_bin: str = "/usr/local/bin/wg-obfuscator"
    config_version: int = 1
    updated_at: str = ""

    def __post_init__(self) -> None:
        self.masking = Masking(self.masking)

    def validate(self) -> "Settings":
        if not SPOKE_MIN <= self.spokes <= SPOKE_MAX:
            raise ValidationError(f"spokes вне диапазона {SPOKE_MIN}..{SPOKE_MAX}: {self.spokes}")
        if not MTU_MIN <= self.mtu <= MTU_MAX:
            raise ValidationError(f"mtu вне диапазона {MTU_MIN}..{MTU_MAX}: {self.mtu}")
        if not 1024 <= self.port_base <= 65535 - SPOKE_MAX:
            raise ValidationError(f"port_base вне диапазона 1024..{65535 - SPOKE_MAX}: {self.port_base}")
        self._check_port_collisions()
        if not OBFUSCATION_KEY_RE.match(self.obfuscation_key or ""):
            # Значение ключа в сообщение не попадает.
            raise ValidationError(
                "obfuscation_key: допустимы 8-255 символов из [A-Za-z0-9_-.@+:/]"
            )
        if self.external_host and not _is_valid_host(self.external_host):
            raise ValidationError(f"external_host: некорректный адрес {self.external_host!r}")
        if not self.obfuscator_bin.startswith("/"):
            raise ValidationError("obfuscator_bin: нужен абсолютный путь")
        return self

    def _check_port_collisions(self) -> None:
        """Диапазон обфускаторов не должен пересекаться с чужими портами.

        Проверяется весь 1..SPOKE_MAX, а не текущее число лучей: иначе увеличение
        spokes позже наложило бы обфускатор на WireGuard уже без всякой валидации.
        Наложение фатально и молча: `_reconcile_interface()` отрабатывает раньше,
        WireGuard занимает порт, обфускатор навсегда падает на bind.
        """
        obf_ports = {server_obf_port(i, self.port_base) for i in range(SPOKE_MIN, SPOKE_MAX + 1)}
        wg_ports = {wg_port(i) for i in range(SPOKE_MIN, SPOKE_MAX + 1)}

        clash = sorted(obf_ports & wg_ports)
        if clash:
            raise ValidationError(
                f"port_base={self.port_base}: порты обфускаторов {clash} совпадают с портами "
                f"WireGuard ({WG_PORT_BASE}+i); выберите базу вне {WG_PORT_BASE - SPOKE_MAX}"
                f"..{WG_PORT_BASE + SPOKE_MAX}"
            )

        service_port = api_port()
        if service_port in obf_ports:
            raise ValidationError(
                f"port_base={self.port_base}: луч занял бы порт управляющего API {service_port}"
            )

    @property
    def spoke_indexes(self) -> list[int]:
        return list(range(SPOKE_MIN, self.spokes + 1))

    def obf_port(self, index: int) -> int:
        return server_obf_port(index, self.port_base)

    def to_api(self) -> dict[str, Any]:
        """Ровно те поля, что перечислены в SPEC для GET /api/settings."""
        return {
            "spokes": self.spokes,
            "masking": self.masking.value,
            "mtu": self.mtu,
            "external_host": self.external_host,
            "port_base": self.port_base,
            "key": self.obfuscation_key,
        }

    def safe_dict(self) -> dict[str, Any]:
        """Полное представление без значения ключа — для логов и /api/status."""
        data = self.to_api()
        data["key"] = MASKED if self.obfuscation_key else ""
        data["obfuscator_bin"] = self.obfuscator_bin
        data["config_version"] = self.config_version
        data["updated_at"] = self.updated_at
        return data


@dataclass
class Spoke:
    """Один луч: WireGuard swg{i} + серверный обфускатор."""

    index: int
    listen_port: int
    obf_port: int
    server_ip: str
    subnet: str
    private_key: str = field(repr=False, default="")
    public_key: str = ""
    enabled: bool = True
    created_at: str = ""

    @property
    def iface(self) -> str:
        return iface_name(self.index)

    @property
    def client_port(self) -> int:
        return client_local_port(self.index)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "iface": self.iface,
            "listen_port": self.listen_port,
            "obf_port": self.obf_port,
            "client_port": self.client_port,
            "server_ip": self.server_ip,
            "subnet": self.subnet,
            "public_key": self.public_key,
            "private_key": MASKED if self.private_key else "",
            "enabled": self.enabled,
            "created_at": self.created_at,
        }


@dataclass
class ClientKey:
    """Ключевая пара клиента для одного луча."""

    spoke_index: int
    private_key: str = field(repr=False, default="")
    public_key: str = ""
    address: str = ""

    @property
    def address_cidr(self) -> str:
        return f"{self.address}/30"


@dataclass
class Client:
    id: int
    name: str
    slot: int
    # Первые 8 hex-символов sha256 от токена. Несекретно: по нему нельзя ни
    # восстановить токен, ни сократить перебор, но «этот ли токен у клиента»
    # проверяется одной командой (см. README).
    token_id: str = ""
    token_hash: str = field(repr=False, default="")
    created_at: str = ""
    keys: dict[int, ClientKey] = field(default_factory=dict, repr=False)

    def address(self, index: int) -> str:
        return client_address(index, self.slot)

    def safe_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "slot": self.slot,
            "token_id": self.token_id,
            "created_at": self.created_at,
            "addresses": {
                str(idx): key.address for idx, key in sorted(self.keys.items())
            },
        }


@dataclass
class BundleSpoke:
    index: int
    server_port: int
    local_port: int
    wg_private_key: str = field(repr=False, default="")
    wg_server_pubkey: str = ""
    address: str = ""
    peer_address: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "server_port": self.server_port,
            "local_port": self.local_port,
            "wg_private_key": self.wg_private_key,
            "wg_server_pubkey": self.wg_server_pubkey,
            "address": self.address,
            "peer_address": self.peer_address,
        }


@dataclass
class Bundle:
    """Клиентский бандл. Формат зафиксирован SPEC, раздел «Формат бандла».

    Поля SPEC не переименовываются и не удаляются: их разбирает jsonfilter по
    фиксированным путям. Полей `agg_mode` и `agg_address` в бандле нет: режимов
    агрегации не существует, общего адреса не существует. Это ломающее изменение
    для клиента 1.1.0 — порядок выката задан PACKAGING.md, 4.3: клиент, потом
    сервер.

    Every spoke stands on its own: `address` is the only address its peer
    accepts, so the client keeps it on owg{i} with a routing table of its own
    and a consumer picks a spoke by binding to that interface. No address is
    shared between spokes and no return path is spread over several of them -
    both were measured to kill the traffic.
    """

    config_version: int
    host: str
    masking: Masking
    mtu: int
    obfuscation_key: str = field(repr=False, default="")
    spokes: list[BundleSpoke] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_version": self.config_version,
            "server": {
                "host": self.host,
                "masking": Masking(self.masking).value,
                "mtu": self.mtu,
            },
            "obfuscation_key": self.obfuscation_key,
            "spokes": [s.to_dict() for s in self.spokes],
        }

    def safe_dict(self) -> dict[str, Any]:
        """То же самое, но без ключей — для логов и отладочного вывода."""
        data = self.to_dict()
        data["obfuscation_key"] = MASKED if self.obfuscation_key else ""
        for spoke in data["spokes"]:
            spoke["wg_private_key"] = MASKED if spoke["wg_private_key"] else ""
        return data


def _is_valid_host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    return bool(HOST_RE.match(value))
