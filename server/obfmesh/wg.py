"""Обёртки над системными утилитами (ip, wg, iptables, sysctl) и генерация ключей.

Все внешние команды идут через `run()`. При OBFMESH_DRY_RUN=1 команды не выполняются,
а пишутся в лог — это позволяет гонять reconcile() и тесты без root и без Linux.

Ключи никогда не передаются аргументами командной строки (их видно в `ps`): приватный
ключ уходит в `wg set` через временный файл с правами 600.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import secrets
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .models import MASKED, ObfmeshError, WG_KEY_RE

log = logging.getLogger("obfmesh.wg")

_TOOL_DIRS = ("/usr/sbin", "/sbin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/local/bin")

_TRUE_VALUES = {"1", "true", "yes", "on"}


class CommandError(ObfmeshError):
    """Команда завершилась с ненулевым кодом."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{shlex.join(self.argv)} -> rc={returncode}: {stderr.strip()}")


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    executed: bool = True

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def dry_run() -> bool:
    """Читается на каждом вызове: тесты переключают режим через окружение."""
    return os.environ.get("OBFMESH_DRY_RUN", "").strip().lower() in _TRUE_VALUES


def tool(name: str) -> str:
    """Абсолютный путь к утилите. PATH у systemd-юнита может не содержать /usr/sbin."""
    found = shutil.which(name)
    if found:
        return found
    for directory in _TOOL_DIRS:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return name


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    timeout: float = 15.0,
    dry_returncode: int = 0,
    dry_stdout: str = "",
) -> CommandResult:
    """Выполнить команду. В dry-run — только записать в лог.

    `dry_returncode`/`dry_stdout` задают ответ пробы в dry-run: пробы состояния
    отвечают «ничего нет», чтобы reconcile() показал полный список действий.
    """
    argv = [str(a) for a in argv]
    if dry_run():
        log.info("dry-run: %s", shlex.join(argv))
        return CommandResult(argv, dry_returncode, dry_stdout, "", executed=False)

    log.debug("exec: %s", shlex.join(argv))
    try:
        proc = subprocess.run(  # noqa: S603 - argv собирается здесь, shell не используется
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        result = CommandResult(argv, 127, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        result = CommandResult(argv, 124, "", f"timeout after {exc.timeout}s")
    else:
        # stdout не логируется никогда: `wg show ... dump` печатает приватный ключ.
        result = CommandResult(argv, proc.returncode, proc.stdout, proc.stderr)

    if not result.ok:
        log.debug("failed: %s rc=%s stderr=%s", shlex.join(argv), result.returncode, result.stderr.strip())
    if check and not result.ok:
        raise CommandError(argv, result.returncode, result.stderr)
    return result


# --- Ключи X25519 -----------------------------------------------------------
#
# Реализация RFC 7748 вместо вызова `wg genkey`: результат идентичен, но не зависит
# от наличия бинарника wg — иначе dry-run и тесты требовали бы Linux.

_P = 2**255 - 19
_A24 = 121665


def _clamp(scalar: bytes) -> bytes:
    data = bytearray(scalar)
    data[0] &= 248
    data[31] &= 127
    data[31] |= 64
    return bytes(data)


def _x25519(scalar: bytes, u_bytes: bytes) -> bytes:
    k = int.from_bytes(_clamp(scalar), "little")
    x1 = int.from_bytes(u_bytes, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3 = 1, 0, x1, 1
    swap = 0
    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_t
        a = (x2 + z2) % _P
        aa = a * a % _P
        b = (x2 - z2) % _P
        bb = b * b % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = d * a % _P
        cb = c * b % _P
        x3 = (da + cb) % _P
        x3 = x3 * x3 % _P
        z3 = (da - cb) % _P
        z3 = z3 * z3 % _P
        z3 = x1 * z3 % _P
        x2 = aa * bb % _P
        z2 = e * ((aa + _A24 * e) % _P) % _P
    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


def genkey() -> str:
    """Приватный ключ WireGuard в base64 (32 байта, clamped — как у `wg genkey`)."""
    return base64.b64encode(_clamp(secrets.token_bytes(32))).decode("ascii")


def pubkey(private_key: str) -> str:
    """Публичный ключ из приватного (X25519 от базовой точки 9)."""
    raw = base64.b64decode(private_key, validate=True)
    if len(raw) != 32:
        raise ObfmeshError("приватный ключ WireGuard должен быть 32 байта")
    basepoint = (9).to_bytes(32, "little")
    return base64.b64encode(_x25519(raw, basepoint)).decode("ascii")


def keypair() -> tuple[str, str]:
    private = genkey()
    return private, pubkey(private)


def is_valid_key(value: str) -> bool:
    return bool(value) and bool(WG_KEY_RE.match(value))


# --- Интерфейсы -------------------------------------------------------------


@dataclass
class WgPeer:
    public_key: str
    endpoint: str = ""
    allowed_ips: list[str] = field(default_factory=list)
    latest_handshake: int = 0
    rx_bytes: int = 0
    tx_bytes: int = 0
    keepalive: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "public_key": self.public_key,
            "endpoint": self.endpoint,
            "allowed_ips": list(self.allowed_ips),
            "latest_handshake": self.latest_handshake,
            "rx_bytes": self.rx_bytes,
            "tx_bytes": self.tx_bytes,
            "keepalive": self.keepalive,
        }


@dataclass
class WgInterface:
    name: str
    public_key: str = ""
    listen_port: int = 0
    fwmark: str = "off"
    peers: list[WgPeer] = field(default_factory=list)

    def peer(self, public_key: str) -> WgPeer | None:
        for item in self.peers:
            if item.public_key == public_key:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "public_key": self.public_key,
            "private_key": MASKED,
            "listen_port": self.listen_port,
            "fwmark": self.fwmark,
            "peers": [p.to_dict() for p in self.peers],
        }


def interface_exists(name: str) -> bool:
    return run([tool("ip"), "link", "show", "dev", name], check=False, dry_returncode=1).ok


def link_info(name: str) -> dict[str, Any] | None:
    result = run([tool("ip"), "-j", "link", "show", "dev", name], check=False, dry_returncode=1)
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None
    return data[0] if data else None


def link_is_up(name: str) -> bool:
    info = link_info(name)
    return bool(info) and "UP" in (info.get("flags") or [])


def interface_addresses(name: str) -> list[str]:
    """Список IPv4-адресов интерфейса в виде a.b.c.d/len."""
    result = run([tool("ip"), "-j", "-4", "addr", "show", "dev", name], check=False, dry_returncode=1)
    if not result.ok:
        return []
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    addresses: list[str] = []
    for entry in data:
        for info in entry.get("addr_info") or []:
            if info.get("family") == "inet" and info.get("local"):
                addresses.append(f"{info['local']}/{info.get('prefixlen', 32)}")
    return addresses


def create_interface(name: str) -> None:
    run([tool("ip"), "link", "add", "dev", name, "type", "wireguard"])


def delete_interface(name: str) -> None:
    run([tool("ip"), "link", "del", "dev", name])


def set_link(name: str, *, mtu: int | None = None, up: bool = False) -> None:
    argv = [tool("ip"), "link", "set", "dev", name]
    if mtu is not None:
        argv += ["mtu", str(mtu)]
    if up:
        argv.append("up")
    run(argv)


def add_address(name: str, cidr: str) -> None:
    run([tool("ip"), "-4", "address", "add", cidr, "dev", name])


def del_address(name: str, cidr: str) -> None:
    run([tool("ip"), "-4", "address", "del", cidr, "dev", name])


def set_private_key(name: str, private_key: str) -> None:
    """Приватный ключ отдаётся wg через временный файл 600, не через argv."""
    if dry_run():
        log.info("dry-run: %s set %s private-key <файл>", tool("wg"), name)
        return
    fd, path = tempfile.mkstemp(prefix="obfmesh-", suffix=".key")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(private_key + "\n")
        run([tool("wg"), "set", name, "private-key", path])
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def set_listen_port(name: str, port: int) -> None:
    run([tool("wg"), "set", name, "listen-port", str(port)])


def set_peer(name: str, public_key: str, allowed_ips: Iterable[str]) -> None:
    run(
        [
            tool("wg"),
            "set",
            name,
            "peer",
            public_key,
            "allowed-ips",
            ",".join(allowed_ips),
        ]
    )


def remove_peer(name: str, public_key: str) -> None:
    run([tool("wg"), "set", name, "peer", public_key, "remove"])


def show_dump(name: str) -> WgInterface | None:
    """Разбор `wg show <dev> dump`.

    Первое поле первой строки — приватный ключ интерфейса; он сознательно
    отбрасывается и никуда не сохраняется.
    """
    result = run([tool("wg"), "show", name, "dump"], check=False, dry_returncode=1)
    if not result.ok:
        return None
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None

    head = lines[0].split("\t")
    iface = WgInterface(name=name)
    if len(head) >= 4:
        iface.public_key = head[1]
        iface.listen_port = _int(head[2])
        iface.fwmark = head[3]

    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) < 8:
            continue
        allowed = [item for item in parts[3].split(",") if item and item != "(none)"]
        endpoint = parts[2] if parts[2] != "(none)" else ""
        iface.peers.append(
            WgPeer(
                public_key=parts[0],
                endpoint=endpoint,
                allowed_ips=allowed,
                latest_handshake=_int(parts[4]),
                rx_bytes=_int(parts[5]),
                tx_bytes=_int(parts[6]),
                keepalive=_int(parts[7]) if parts[7] not in ("off", "") else 0,
            )
        )
    return iface


# --- маршруты ---------------------------------------------------------------


def _route_line(line: str) -> tuple[str, list[str]]:
    """Разбор строки `ip -o route show`: (префикс, устройства).

    В однострочном выводе плечи многопутевого маршрута отделены обратной косой;
    после её замены на пробел все `dev X` лежат подряд. Пустой список устройств
    — это маршрут, в который ничего не отправить: ровно то, что видели на бою.
    """
    parts = line.replace("\\", " ").split()
    if not parts:
        return "", []
    # Тип маршрута (blackhole, unreachable и прочие) стоит перед префиксом.
    head = parts[1] if parts[0].isalpha() and len(parts) > 1 else parts[0]
    devices = [
        parts[position + 1]
        for position, token_ in enumerate(parts)
        if token_ == "dev" and position + 1 < len(parts)
    ]
    try:
        prefix = str(ipaddress.ip_network(head, strict=False))
    except ValueError:
        return "", devices
    return prefix, devices


def route_devices(cidr: str) -> list[str]:
    """Устройства маршрута ровно для этого префикса. Пусто — маршрута нет.

    `to exact` обязателен: без него фильтр показал бы и покрывающий connected-
    маршрут интерфейса, и снятие/сравнение работали бы не с тем маршрутом.
    """
    result = run(
        [tool("ip"), "-o", "route", "show", "to", "exact", cidr],
        check=False,
        dry_returncode=0,
    )
    if not result.ok:
        return []
    devices: list[str] = []
    for line in result.stdout.splitlines():
        devices.extend(_route_line(line)[1])
    return devices


def route_table(prefix: str) -> dict[str, list[str]]:
    """Снимок маршрутов внутри `prefix` (таблица main): префикс -> устройства.

    Один вызов `ip` на весь проход вместо пары на каждого клиента, и заодно
    согласованный срез — сравнение и уборка смотрят на одно и то же состояние.
    """
    result = run(
        [tool("ip"), "-o", "route", "show", "to", "root", prefix],
        check=False,
        dry_returncode=0,
    )
    if not result.ok:
        return {}
    table: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        route_prefix, devices = _route_line(line)
        if route_prefix:
            table[route_prefix] = devices
    return table


def delete_route(cidr: str) -> None:
    run([tool("ip"), "route", "del", cidr], check=False)


# --- iptables и sysctl ------------------------------------------------------


def iptables_rule_exists(table: str, chain: str, rule: Sequence[str]) -> bool:
    argv = [tool("iptables"), "-w", "5", "-t", table, "-C", chain, *rule]
    return run(argv, check=False, dry_returncode=1).ok


def iptables_append(table: str, chain: str, rule: Sequence[str]) -> None:
    run([tool("iptables"), "-w", "5", "-t", table, "-A", chain, *rule])


def iptables_insert(table: str, chain: str, rule: Sequence[str], position: int = 1) -> None:
    run([tool("iptables"), "-w", "5", "-t", table, "-I", chain, str(position), *rule])


def iptables_delete(table: str, chain: str, rule: Sequence[str]) -> None:
    run([tool("iptables"), "-w", "5", "-t", table, "-D", chain, *rule])


def ip_forward_enabled() -> bool:
    if dry_run():
        return False
    try:
        with open("/proc/sys/net/ipv4/ip_forward", "r", encoding="ascii") as handle:
            return handle.read().strip() == "1"
    except OSError:
        return False


def enable_ip_forward() -> None:
    if dry_run():
        log.info("dry-run: sysctl -w net.ipv4.ip_forward=1")
        return
    with open("/proc/sys/net/ipv4/ip_forward", "w", encoding="ascii") as handle:
        handle.write("1\n")


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
