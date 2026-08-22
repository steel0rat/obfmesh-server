"""reconcile(): приведение системы к желаемому состоянию.

Для каждого луча i (1..spokes):
  * WireGuard swg{i}: адрес 10.77.{i}.1/24, порт 51820+i, MTU из настроек;
  * пиры swg{i} — ровно клиенты из базы, лишние снимаются;
  * процесс wg-obfuscator с конфигом /etc/obfmesh/obf{i}.conf:
    слушает port_base+i, отдаёт в 127.0.0.1:51820+i, маскировка STUN;
  * NAT: MASQUERADE для 10.77.0.0/16 и разрешающие правила FORWARD.

Spokes are symmetric and independent. A peer on swg{i} is allowed exactly one
address, 10.77.{i}.{4k+2}/32, and the reply to it leaves through the connected
route of swg{i} - the very interface the request arrived on. There is no address
shared between spokes and no return route spread over several of them: measured
on live machines, that scheme delivered traffic over exactly one spoke, and
which one changed from run to run, because a client socket bound to a spoke
drops packets that came in through another one. Aggregation is done by the
consumers instead - different services bind to different owg{i}.

Уменьшение числа лучей гасит только лишние: работающие лучи не трогаются,
ключи погашенных остаются в базе, поэтому повторное включение не меняет бандлы.

Пути переопределяются окружением: OBFMESH_ETC_DIR, OBFMESH_RUN_DIR, OBFMESH_LOG_DIR.
При OBFMESH_DRY_RUN=1 системные команды и запуск процессов только логируются;
файлы конфигов при этом пишутся по-настоящему (в тестовые каталоги).
"""

from __future__ import annotations

import ipaddress
import logging
import os
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import wg
from .config_gen import render_obfuscator_config
from .db import Database, get_db
from .models import (
    MESH_NET,
    SPOKE_MAX,
    Masking,
    ObfmeshError,
    Settings,
    Spoke,
    iface_name,
    server_address_cidr,
)

log = logging.getLogger("obfmesh.orchestrator")

DEFAULT_ETC_DIR = "/etc/obfmesh"
DEFAULT_RUN_DIR = "/run/obfmesh"
DEFAULT_LOG_DIR = "/var/log/obfmesh"

SUPERVISE_INTERVAL = 2.0  # период опроса живости обфускаторов, секунды
RESTART_BACKOFF_MAX = 30.0
HEALTHY_UPTIME = 60.0  # после стольких секунд работы счётчик падений сбрасывается
STOP_GRACE = 5.0
# Обфускатор с занятым портом или отвергнутым конфигом умирает сразу. Без этой
# паузы _start_obfuscator() рапортовал бы «запущен» про уже мёртвый pid.
START_SETTLE = 0.7

MESH_CIDR = str(MESH_NET)

# Network of the aggregate client addresses of the previous scheme. Spoke i owns
# 10.77.{i}.0/24 for i >= 1, so 10.77.0.0/24 belongs to nothing now and every
# route inside it is a leftover of the multipath return path. Swept on every
# pass: an upgraded server must not keep sending replies into a dead route.
LEGACY_AGG_NET = ipaddress.IPv4Network("10.77.0.0/24")

# Journal of the routes that scheme installed, written by earlier releases.
LEGACY_ROUTES_FILE = "client-routes.tab"

# Ошибки, из-за которых нельзя ронять весь проход: sqlite3.Error не наследуется
# ни от ObfmeshError, ни от OSError, а «database is locked» сверх busy_timeout
# случается на каждом параллельном PATCH.
RECOVERABLE = (ObfmeshError, OSError, sqlite3.Error)

# Правила, нужные для выхода клиентов в интернет. FORWARD задан явно: на Ubuntu
# с установленным docker политика цепочки FORWARD — DROP.
_BASE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("nat", "POSTROUTING", ("-s", MESH_CIDR, "!", "-d", MESH_CIDR, "-j", "MASQUERADE")),
    ("filter", "FORWARD", ("-s", MESH_CIDR, "-j", "ACCEPT")),
    ("filter", "FORWARD", ("-d", MESH_CIDR, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT")),
)

_TRUE_VALUES = {"1", "true", "yes", "on"}


def etc_dir() -> str:
    return os.environ.get("OBFMESH_ETC_DIR", "").strip() or DEFAULT_ETC_DIR


def run_dir() -> str:
    return os.environ.get("OBFMESH_RUN_DIR", "").strip() or DEFAULT_RUN_DIR


def log_dir() -> str:
    return os.environ.get("OBFMESH_LOG_DIR", "").strip() or DEFAULT_LOG_DIR


def manage_input_rules() -> bool:
    """INPUT-правила для портов обфускаторов — по явному включению.

    По умолчанию выключено: на сервере может быть свой firewall со своим порядком
    цепочек, и молча вставлять туда правила неправильно.
    """
    return os.environ.get("OBFMESH_MANAGE_INPUT", "").strip().lower() in _TRUE_VALUES


def stop_processes_on_exit() -> bool:
    """Гасить ли обфускаторы при остановке сервиса. По умолчанию — нет.

    KillMode=process в юните и этот флаг — одно решение: перезапуск управляющего
    сервиса не должен рвать туннели. Полная остановка — `obfmesh-ctl teardown`
    либо OBFMESH_STOP_PROCESSES=1 в drop-in юнита.
    """
    return os.environ.get("OBFMESH_STOP_PROCESSES", "").strip().lower() in _TRUE_VALUES


def periodic_reconcile_interval() -> float:
    """Период фонового reconcile в секундах, 0 — выключено.

    Нужен, чтобы система сама чинилась после внешних вмешательств (перезапуск
    docker или firewall вычищает правила FORWARD/NAT).
    """
    raw = os.environ.get("OBFMESH_RECONCILE_INTERVAL", "").strip()
    if not raw:
        return 300.0
    try:
        value = float(raw)
    except ValueError:
        return 300.0
    return max(0.0, value)


@dataclass
class ReconcileReport:
    """Итог прогона.

    `changes` — изменения постоянного состояния (база, файлы конфигов). При
    неизменных настройках повторный reconcile() оставляет список пустым.
    `actions` — выполненные системные команды. В dry-run состояние системы
    непроверяемо, поэтому там список повторяется от прогона к прогону.
    """

    changes: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    spokes: list[int] = field(default_factory=list)
    config_version: int = 0
    dry_run: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "changes": list(self.changes),
            "actions": list(self.actions),
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "spokes": list(self.spokes),
            "config_version": self.config_version,
            "dry_run": self.dry_run,
            "duration_ms": self.duration_ms,
        }


@dataclass
class ObfProcess:
    """Запущенный (или подобранный по pid-файлу) обфускатор одного луча."""

    index: int
    pid: int
    binary: str
    popen: subprocess.Popen | None = None
    adopted: bool = False
    started_at: float = 0.0

    def alive(self) -> bool:
        if self.popen is not None:
            return self.popen.poll() is None
        return _pid_alive(self.pid)


class Orchestrator:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or get_db()
        self._lock = threading.RLock()
        self._procs: dict[int, ObfProcess] = {}
        self._backoff: dict[int, tuple[int, float]] = {}  # index -> (падений, время следующей попытки)
        self._supervisor: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_reconcile: float = 0.0

    @property
    def db(self) -> Database:
        return self._db

    # --- главный проход ----------------------------------------------------

    def reconcile(self) -> ReconcileReport:
        started = time.monotonic()
        with self._lock:
            report = ReconcileReport(dry_run=wg.dry_run())
            try:
                settings = self._db.get_settings()
            except RECOVERABLE as exc:
                report.errors.append(f"настройки недоступны: {exc}")
                log.error("настройки недоступны: %s", exc)
                return report

            report.spokes = settings.spoke_indexes
            if settings.masking is Masking.NONE:
                # Замер: провайдер душит и чистый WireGuard, и обфускацию без
                # маскировки. Настройка разрешена, но молчать о ней нельзя.
                report.warnings.append(
                    "masking=NONE: обфускация без маскировки режется провайдером, "
                    "рабочее значение — STUN"
                )

            # Пролог тоже под перехватом: раньше падение _ensure_dirs() или
            # блокировка SQLite в ensure_client_keys() роняли весь проход,
            # и ни один луч не сверялся вовсе.
            try:
                self._ensure_dirs(report)
            except RECOVERABLE as exc:
                report.errors.append(f"каталоги: {exc}")
                log.error("каталоги: %s", exc)

            bundle_touched = False
            try:
                if self._db.ensure_client_keys(settings.spoke_indexes):
                    bundle_touched = True
                    report.changes.append("клиентам догенерированы недостающие ключи")
            except RECOVERABLE as exc:
                report.errors.append(f"ключи клиентов: {exc}")
                log.error("ключи клиентов: %s", exc)

            for index in settings.spoke_indexes:
                try:
                    spoke, changes = self._db.ensure_spoke(index, settings.port_base)
                    if changes:
                        bundle_touched = True
                        report.changes.extend(changes)
                    self._reconcile_spoke(spoke, settings, report)
                # Сбой одного луча не должен мешать остальным: ошибка попадает в отчёт.
                except RECOVERABLE as exc:
                    report.errors.append(f"луч {index}: {exc}")
                    log.error("луч %s: %s", index, exc)

            for index in range(settings.spokes + 1, SPOKE_MAX + 1):
                try:
                    if self._teardown_spoke(index, settings, report):
                        bundle_touched = True
                except RECOVERABLE as exc:
                    report.errors.append(f"луч {index}: снятие не удалось: {exc}")
                    log.error("луч %s: снятие не удалось: %s", index, exc)

            try:
                self._sweep_legacy_aggregate_routes(report)
            except RECOVERABLE as exc:
                report.errors.append(f"уборка маршрутов прежней схемы: {exc}")
                log.error("уборка маршрутов прежней схемы: %s", exc)

            try:
                self._reconcile_nat(settings, report)
            except RECOVERABLE as exc:
                report.errors.append(f"NAT: {exc}")
                log.error("NAT: %s", exc)

            try:
                if bundle_touched:
                    report.config_version = self._db.bump_config_version()
                else:
                    report.config_version = self._db.get_config_version()
            except RECOVERABLE as exc:
                report.errors.append(f"config_version: {exc}")
                log.error("config_version: %s", exc)

            self._last_reconcile = time.monotonic()
            report.duration_ms = int((time.monotonic() - started) * 1000)
            log.info(
                "reconcile: лучей %s, изменений %s, действий %s, ошибок %s, config_version=%s",
                len(report.spokes),
                len(report.changes),
                len(report.actions),
                len(report.errors),
                report.config_version,
            )
            return report

    # --- шаги --------------------------------------------------------------

    def _ensure_dirs(self, report: ReconcileReport) -> None:
        for path, mode in ((etc_dir(), 0o700), (run_dir(), 0o700), (log_dir(), 0o700)):
            if not os.path.isdir(path):
                os.makedirs(path, mode=mode, exist_ok=True)
                report.changes.append(f"создан каталог {path}")
            current = os.stat(path).st_mode & 0o777
            if current != mode:
                os.chmod(path, mode)
                report.changes.append(f"права каталога {path}: {current:o} -> {mode:o}")

    def _reconcile_spoke(self, spoke: Spoke, settings: Settings, report: ReconcileReport) -> None:
        dump = self._reconcile_interface(spoke, settings, report)
        self._reconcile_peers(spoke, dump, report)
        self._reconcile_obfuscator(spoke, settings, report)

    def _reconcile_interface(
        self, spoke: Spoke, settings: Settings, report: ReconcileReport
    ) -> wg.WgInterface | None:
        name = spoke.iface
        if not wg.interface_exists(name):
            wg.create_interface(name)
            report.actions.append(f"{name}: интерфейс создан")

        desired_cidr = server_address_cidr(spoke.index)
        addresses = wg.interface_addresses(name)
        if desired_cidr not in addresses:
            wg.add_address(name, desired_cidr)
            report.actions.append(f"{name}: адрес {desired_cidr} назначен")
        for address in addresses:
            if address == desired_cidr:
                continue
            try:
                inside_mesh = ipaddress.ip_interface(address).ip in MESH_NET
            except ValueError:
                continue
            if inside_mesh:  # чужие адреса вне 10.77/16 не трогаем
                wg.del_address(name, address)
                report.actions.append(f"{name}: лишний адрес {address} снят")

        info = wg.link_info(name)
        if info is None or info.get("mtu") != settings.mtu:
            wg.set_link(name, mtu=settings.mtu)
            report.actions.append(f"{name}: MTU {settings.mtu}")

        # Приватный ключ интерфейса сравнивается через публичный: читать
        # приватный из `wg show` незачем.
        dump = wg.show_dump(name)
        if dump is None or dump.public_key != spoke.public_key:
            wg.set_private_key(name, spoke.private_key)
            report.actions.append(f"{name}: приватный ключ установлен")
        if dump is None or dump.listen_port != spoke.listen_port:
            wg.set_listen_port(name, spoke.listen_port)
            report.actions.append(f"{name}: порт {spoke.listen_port}")

        if info is None or "UP" not in (info.get("flags") or []):
            wg.set_link(name, up=True)
            report.actions.append(f"{name}: поднят")

        # rp_filter is left at the distribution default on purpose. A packet
        # reaches swg{i} only from a peer whose allowed-ips is 10.77.{i}.x/32,
        # and the route back to that address is the connected route of the same
        # interface, so even strict reverse path validation lets it through.
        return dump

    def _reconcile_peers(
        self, spoke: Spoke, dump: wg.WgInterface | None, report: ReconcileReport
    ) -> None:
        name = spoke.iface
        desired = self._db.client_peers(spoke.index)
        actual = {peer.public_key: peer for peer in dump.peers} if dump else {}

        for public_key, info in desired.items():
            allowed = self._allowed_ips_for(info)
            peer = actual.get(public_key)
            if peer is None or set(peer.allowed_ips) != set(allowed):
                wg.set_peer(name, public_key, allowed)
                report.actions.append(
                    f"{name}: пир клиента {info['client']} ({','.join(allowed)})"
                )

        for public_key in actual:
            if public_key not in desired:
                wg.remove_peer(name, public_key)
                report.actions.append(f"{name}: снят посторонний пир")

    def _allowed_ips_for(self, info: dict[str, Any]) -> list[str]:
        """AllowedIPs пира клиента на одном луче: ровно его адрес на этом луче.

        Nothing else may appear here. An address shared between spokes made the
        server pick the reply spoke by its own hash, while the client socket was
        bound to one specific spoke and dropped whatever arrived through another
        one - measured as "one spoke carries everything, the rest are at zero,
        and which one it is changes between runs".
        """
        return [f"{info['address']}/32"]

    def _sweep_legacy_aggregate_routes(self, report: ReconcileReport) -> None:
        """Remove the return routes of the aggregate-address scheme.

        Those routes live in the kernel until reboot, and the journal of them
        lives in /run, so a server upgraded in place keeps both. Everything
        inside 10.77.0.0/24 is swept: the network is not on any interface any
        more, so nothing legitimate can be there. Idempotent - once the sweep is
        done the pass finds nothing and reports nothing.
        """
        journal = self.legacy_routes_path()
        recorded = _read_line_list(journal)
        actual = wg.route_table(str(LEGACY_AGG_NET))

        for cidr in sorted(recorded | set(actual)):
            # A journal entry of an even older scheme can point outside the
            # aggregate network, so its presence has to be asked about directly.
            if cidr in actual or wg.route_devices(cidr):
                wg.delete_route(cidr)
                report.actions.append(f"маршрут прежней схемы {cidr} снят")

        # In dry-run nothing was removed and the routing table is not readable,
        # so the journal stays: it is the only record of the entries that lie
        # outside the aggregate network and cannot be found by the sweep.
        if not wg.dry_run() and os.path.exists(journal):
            _remove_file(journal)
            report.changes.append(f"журнал маршрутов прежней схемы {journal} удалён")

    def _reconcile_obfuscator(self, spoke: Spoke, settings: Settings, report: ReconcileReport) -> None:
        index = spoke.index
        config_path = self.config_path(index)
        content = render_obfuscator_config(index, spoke.obf_port, spoke.listen_port, settings)
        if _write_file_if_changed(config_path, content, 0o600):
            report.changes.append(f"луч {index}: конфиг {config_path} обновлён")
            config_changed = True
        else:
            config_changed = False

        if wg.dry_run():
            log.info(
                "dry-run: %s -c %s (луч %s: :%s -> 127.0.0.1:%s)",
                settings.obfuscator_bin,
                config_path,
                index,
                spoke.obf_port,
                spoke.listen_port,
            )
            report.actions.append(f"луч {index}: обфускатор был бы запущен")
            return

        proc = self._procs.get(index)
        if proc is None:
            proc = self._adopt(index, config_path)
            if proc is not None:
                self._procs[index] = proc
                report.actions.append(f"луч {index}: подобран уже запущенный обфускатор pid={proc.pid}")

        if proc is not None and proc.alive():
            if not config_changed and proc.binary == settings.obfuscator_bin:
                return
            reason = "конфиг изменён" if config_changed else "сменился путь к бинарнику"
            self._stop_obfuscator(index, reason)
            report.actions.append(f"луч {index}: обфускатор остановлен ({reason})")

        self._start_obfuscator(index, settings, report)

    def _teardown_spoke(self, index: int, settings: Settings, report: ReconcileReport) -> bool:
        """Погасить лишний луч. Возвращает True, если менялось состояние базы."""
        name = iface_name(index)
        config_path = self.config_path(index)

        if not wg.dry_run():
            proc = self._procs.get(index) or self._adopt(index, config_path)
            if proc is not None:
                self._procs[index] = proc
                self._stop_obfuscator(index, "луч выключен")
                report.actions.append(f"луч {index}: обфускатор остановлен")
        elif os.path.exists(self.pid_path(index)):
            report.actions.append(f"луч {index}: обфускатор был бы остановлен")

        self._backoff.pop(index, None)

        if os.path.exists(config_path):
            os.unlink(config_path)
            report.changes.append(f"луч {index}: конфиг {config_path} удалён")

        if wg.interface_exists(name):
            wg.delete_interface(name)
            report.actions.append(f"{name}: интерфейс удалён")

        # INPUT не трогаем: правила ведёт _reconcile_nat() по журналу
        # applied-ports, иначе порт снимался бы по уже выправленной строке БД.

        if self._db.set_spoke_enabled(index, False):
            report.changes.append(f"луч {index}: выключен")
            return True
        return False

    def _reconcile_nat(self, settings: Settings, report: ReconcileReport) -> None:
        if not wg.ip_forward_enabled():
            wg.enable_ip_forward()
            report.actions.append("net.ipv4.ip_forward=1")

        for table, chain, rule in _BASE_RULES:
            if not wg.iptables_rule_exists(table, chain, rule):
                wg.iptables_append(table, chain, rule)
                report.actions.append(f"{table}/{chain}: правило добавлено ({' '.join(rule)})")

        if not manage_input_rules():
            return

        wanted = {settings.obf_port(index) for index in settings.spoke_indexes}
        # Журнал открытых портов: смена port_base иначе оставляла бы в INPUT
        # разрешения на порты, которые уже никто не слушает, — снять их по
        # текущим настройкам невозможно, старое значение нигде не хранится.
        recorded = _read_port_list(self.input_ports_path())

        for port in sorted(recorded - wanted):
            rule = ("-p", "udp", "--dport", str(port), "-j", "ACCEPT")
            if wg.iptables_rule_exists("filter", "INPUT", rule):
                wg.iptables_delete("filter", "INPUT", rule)
                report.actions.append(f"INPUT: разрешение udp/{port} снято")

        for port in sorted(wanted):
            rule = ("-p", "udp", "--dport", str(port), "-j", "ACCEPT")
            if not wg.iptables_rule_exists("filter", "INPUT", rule):
                wg.iptables_insert("filter", "INPUT", rule, position=1)
                report.actions.append(f"INPUT: разрешён udp/{port}")

        if recorded != wanted:
            _write_port_list(self.input_ports_path(), wanted)

    # --- процессы обфускаторов --------------------------------------------

    def config_path(self, index: int) -> str:
        return os.path.join(etc_dir(), f"obf{index}.conf")

    def pid_path(self, index: int) -> str:
        return os.path.join(run_dir(), f"obf{index}.pid")

    def log_path(self, index: int) -> str:
        return os.path.join(log_dir(), f"obf{index}.log")

    def input_ports_path(self) -> str:
        return os.path.join(run_dir(), "input-ports.tab")

    def legacy_routes_path(self) -> str:
        """Journal of the removed scheme; read only to clean up after it."""
        return os.path.join(run_dir(), LEGACY_ROUTES_FILE)

    def _start_obfuscator(self, index: int, settings: Settings, report: ReconcileReport) -> None:
        binary = settings.obfuscator_bin
        if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            raise ObfmeshError(f"обфускатор не найден или не исполняем: {binary}")

        config_path = self.config_path(index)

        # Порт мог остаться за процессом, которого нет в self._procs: reconcile
        # луча упал раньше, чем дошёл до обфускатора, либо сервис перезапустили.
        # Без этой проверки pid-файл переписался бы на новый pid, а старый живой
        # процесс держал бы порт и стал недостижим для остановки.
        running = self._adopt(index, config_path)
        if running is not None:
            self._procs[index] = running
            report.actions.append(f"луч {index}: обфускатор уже работает pid={running.pid}")
            return

        _check_masking_support(binary, settings)

        os.makedirs(log_dir(), mode=0o700, exist_ok=True)
        log_fd = os.open(self.log_path(index), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
        try:
            proc = subprocess.Popen(  # noqa: S603 - аргументы формируются нами, ключей в них нет
                [binary, "-c", config_path],
                stdin=subprocess.DEVNULL,
                stdout=log_fd,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,  # переживает падение управляющего процесса, потом подбирается по pid-файлу
            )
        finally:
            os.close(log_fd)

        # Занятый порт или отвергнутый конфиг убивают обфускатор за миллисекунды.
        # Пауза превращает «записался в pid-файл» в «действительно работает».
        time.sleep(START_SETTLE)
        if proc.poll() is not None:
            raise ObfmeshError(
                f"обфускатор луча {index} умер сразу после старта (код {proc.returncode}); "
                f"журнал: {self.log_path(index)}"
            )

        self._procs[index] = ObfProcess(
            index=index,
            pid=proc.pid,
            binary=binary,
            popen=proc,
            started_at=time.monotonic(),
        )
        _write_pidfile(self.pid_path(index), proc.pid)
        report.actions.append(f"луч {index}: обфускатор запущен pid={proc.pid}")
        log.info("луч %s: обфускатор запущен pid=%s (:%s)", index, proc.pid, settings.obf_port(index))

    def _stop_obfuscator(self, index: int, reason: str) -> None:
        proc = self._procs.pop(index, None)
        if proc is None:
            return
        log.info("луч %s: остановка обфускатора pid=%s (%s)", index, proc.pid, reason)
        try:
            if proc.popen is not None:
                proc.popen.terminate()
                try:
                    proc.popen.wait(timeout=STOP_GRACE)
                except subprocess.TimeoutExpired:
                    proc.popen.kill()
                    proc.popen.wait(timeout=STOP_GRACE)
            else:
                os.kill(proc.pid, signal.SIGTERM)
                deadline = time.monotonic() + STOP_GRACE
                while _pid_alive(proc.pid) and time.monotonic() < deadline:
                    _reap(proc.pid)
                    time.sleep(0.1)
                if _pid_alive(proc.pid):
                    os.kill(proc.pid, signal.SIGKILL)
                    deadline = time.monotonic() + 1.0
                    while _pid_alive(proc.pid) and time.monotonic() < deadline:
                        _reap(proc.pid)
                        time.sleep(0.05)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            log.error("луч %s: нет прав остановить pid=%s: %s", index, proc.pid, exc)
        _remove_file(self.pid_path(index))

    def _adopt(self, index: int, config_path: str, *, prune: bool = True) -> ObfProcess | None:
        """Подобрать обфускатор, переживший перезапуск управляющего процесса.

        Без этого после рестарта сервиса старый процесс продолжал бы держать порт,
        а новый не смог бы забиндиться. `prune=False` — для путей только чтения:
        мёртвый pid-файл при этом остаётся на месте.
        """
        pid = _read_pidfile(self.pid_path(index))
        if pid is None or not _pid_alive(pid):
            if prune:
                _remove_file(self.pid_path(index))
            return None
        cmdline = _read_cmdline(pid)
        if not cmdline or config_path not in cmdline:
            # pid переиспользован другим процессом
            if prune:
                _remove_file(self.pid_path(index))
            return None
        return ObfProcess(
            index=index,
            pid=pid,
            binary=cmdline[0],
            popen=None,
            adopted=True,
            started_at=time.monotonic(),
        )

    # --- надзор ------------------------------------------------------------

    def start_supervisor(self) -> None:
        """Запустить фоновый надзор за обфускаторами. Идемпотентно."""
        with self._lock:
            if self._supervisor is not None and self._supervisor.is_alive():
                return
            if wg.dry_run():
                log.info("dry-run: надзор за процессами не запускается")
                return
            self._stop_event.clear()
            self._supervisor = threading.Thread(
                target=self._supervise_loop, name="obfmesh-supervisor", daemon=True
            )
            self._supervisor.start()
            log.info("надзор за обфускаторами запущен")

    def stop_supervisor(self) -> None:
        thread = self._supervisor
        self._stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=SUPERVISE_INTERVAL * 2)
        self._supervisor = None

    def shutdown(self, *, stop_processes: bool | None = None) -> None:
        """Остановить надзор. Обфускаторы по умолчанию продолжают работать.

        Перезапуск управляющего сервиса не должен рвать туннели: процессы
        переживут его и будут подобраны по pid-файлам. OBFMESH_STOP_PROCESSES=1
        меняет решение на противоположное — тогда `systemctl stop` гасит всё.
        """
        self.stop_supervisor()
        if stop_processes is None:
            stop_processes = stop_processes_on_exit()
        if stop_processes:
            self.teardown_processes()

    def teardown_processes(self) -> list[str]:
        """Погасить все обфускаторы: и свои, и подобранные по pid-файлам.

        Штатный способ остановить лучи после `systemctl stop`: без него процессы
        живут вечно, держат UDP-порты и продолжают форвардить в swg{i}.
        """
        stopped: list[str] = []
        with self._lock:
            for index in range(1, SPOKE_MAX + 1):
                if index not in self._procs:
                    adopted = self._adopt(index, self.config_path(index))
                    if adopted is None:
                        continue
                    self._procs[index] = adopted
                pid = self._procs[index].pid
                self._stop_obfuscator(index, "остановка по запросу")
                stopped.append(f"луч {index}: обфускатор pid={pid} остановлен")
        return stopped

    def _supervise_loop(self) -> None:
        interval = periodic_reconcile_interval()
        while not self._stop_event.wait(SUPERVISE_INTERVAL):
            try:
                self._check_processes()
                if interval and time.monotonic() - self._last_reconcile >= interval:
                    self.reconcile()
            except Exception:  # noqa: BLE001 - поток надзора не имеет права умереть
                log.exception("сбой в потоке надзора")

    def _check_processes(self) -> None:
        """Поднять упавшие обфускаторы. Пауза между попытками растёт до 30 секунд."""
        with self._lock:
            settings = self._db.get_settings()
            now = time.monotonic()
            for index in settings.spoke_indexes:
                proc = self._procs.get(index)
                # Тот же подбор, что и в _reconcile_obfuscator(): reconcile луча
                # мог упасть до запуска обфускатора (например, на `wg set`), и
                # тогда живой процесс отсутствует в self._procs. Без подбора
                # надзор запустил бы второй, тот не забиндился бы на занятый
                # порт, а pid-файл уже указывал бы на него — луч мёртв.
                if proc is None:
                    proc = self._adopt(index, self.config_path(index))
                    if proc is not None:
                        self._procs[index] = proc
                        log.info("луч %s: подобран работающий обфускатор pid=%s", index, proc.pid)

                if proc is not None and proc.alive():
                    # Процесс продержался достаточно долго — считаем попытку удачной.
                    if proc.started_at and now - proc.started_at > HEALTHY_UPTIME:
                        self._backoff.pop(index, None)
                    continue

                if proc is not None:
                    code = proc.popen.returncode if proc.popen is not None else None
                    log.warning("луч %s: обфускатор pid=%s умер (код %s)", index, proc.pid, code)
                    self._procs.pop(index, None)
                    _remove_file(self.pid_path(index))

                attempts, next_retry = self._backoff.get(index, (0, 0.0))
                if now < next_retry:
                    continue
                attempts += 1
                delay = min(RESTART_BACKOFF_MAX, 2.0**min(attempts, 5))
                self._backoff[index] = (attempts, now + delay)
                try:
                    self._start_obfuscator(index, settings, ReconcileReport())
                except RECOVERABLE as exc:
                    log.error(
                        "луч %s: перезапуск не удался (%s), следующая попытка через %.0f с",
                        index,
                        exc,
                        delay,
                    )

    # --- состояние ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Состояние для GET /api/status. Приватные ключи не раскрываются.

        Под локом снимается только слепок базы и таблицы процессов: тот же лок
        держит reconcile() целиком, а он делает до десятка вызовов `ip`/`wg` с
        таймаутом 15 секунд каждый. Опрос системы идёт уже снаружи, поэтому
        GET /api/status не встаёт в очередь за прогоном и ничего не меняет.
        """
        from . import __version__

        with self._lock:
            settings = self._db.get_settings()
            clients = self._db.list_clients()
            db_spokes = self._db.list_spokes()
            peers_known = {spoke.index: self._db.client_peers(spoke.index) for spoke in db_spokes}
            procs = dict(self._procs)
            failures = {index: attempts for index, (attempts, _) in self._backoff.items()}

        spokes: list[dict[str, Any]] = []
        total_rx = total_tx = 0
        latest_overall = 0

        for spoke in db_spokes:
            desired = spoke.index <= settings.spokes
            if not desired and not spoke.enabled:
                continue
            entry = spoke.safe_dict()
            entry["desired"] = desired
            entry["iface_up"] = wg.link_is_up(spoke.iface)
            entry["obfuscator"] = self._process_status(spoke.index, procs, failures)
            entry["config_path"] = self.config_path(spoke.index)

            dump = wg.show_dump(spoke.iface)
            known = peers_known.get(spoke.index, {})
            peers: list[dict[str, Any]] = []
            spoke_rx = spoke_tx = 0
            latest = 0
            if dump is not None:
                entry["listen_port_actual"] = dump.listen_port
                for peer in dump.peers:
                    info = peer.to_dict()
                    info["client"] = known.get(peer.public_key, {}).get("client", "")
                    spoke_rx += peer.rx_bytes
                    spoke_tx += peer.tx_bytes
                    latest = max(latest, peer.latest_handshake)
                    peers.append(info)
            entry["peers"] = peers
            # Счётчики дублируются на уровне луча: UI (static/app.js и страница
            # LuCI) читает их именно отсюда, а не из peers[].
            entry["rx_bytes"] = spoke_rx
            entry["tx_bytes"] = spoke_tx
            entry["latest_handshake"] = latest
            total_rx += spoke_rx
            total_tx += spoke_tx
            latest_overall = max(latest_overall, latest)
            spokes.append(entry)

        return {
            "version": __version__,
            "dry_run": wg.dry_run(),
            "config_version": settings.config_version,
            "spokes_configured": settings.spokes,
            "spokes": spokes,
            "settings": settings.safe_dict(),
            "clients": [client.safe_dict() for client in clients],
            "totals": {
                "rx_bytes": total_rx,
                "tx_bytes": total_tx,
                "latest_handshake": latest_overall,
            },
        }

    def _process_status(
        self,
        index: int,
        procs: dict[int, ObfProcess],
        failures: dict[int, int],
    ) -> dict[str, Any]:
        """Read-only: pid-файлы не удаляются, self._procs не правится.

        Раньше GET /api/status звал _adopt() с побочными эффектами и удалял
        файлы в /run по чужому мёртвому pid.
        """
        proc = procs.get(index)
        if proc is None and not wg.dry_run():
            proc = self._adopt(index, self.config_path(index), prune=False)
        running = bool(proc and proc.alive())
        return {
            "running": running,
            "pid": proc.pid if running and proc else None,
            "adopted": bool(proc.adopted) if proc else False,
            "failures": failures.get(index, 0),
            "log_path": self.log_path(index),
        }


# --- вспомогательное --------------------------------------------------------

# Кеш ответа `wg-obfuscator --help`: (путь, mtime, размер) -> поддерживает masking.
_MASKING_SUPPORT: dict[tuple[str, int, int], bool] = {}
_MASKING_LOCK = threading.Lock()


def _check_masking_support(binary: str, settings: Settings) -> None:
    """Убедиться, что бинарник понимает `masking`, иначе не запускать луч.

    Обфускатор без маскировки поднимается и выглядит здоровым, но провайдер режет
    поток (замер). Старая сборка молча проигнорирует незнакомую строку конфига,
    поэтому её наличие проверяется по собственному --help бинарника.
    OBFMESH_SKIP_MASKING_CHECK=1 отключает проверку, если --help нечитаем.
    """
    if settings.masking is Masking.NONE:
        return
    if os.environ.get("OBFMESH_SKIP_MASKING_CHECK", "").strip().lower() in _TRUE_VALUES:
        return
    try:
        st = os.stat(binary)
    except OSError as exc:
        raise ObfmeshError(f"обфускатор {binary} недоступен: {exc}") from exc

    cache_key = (binary, st.st_mtime_ns, st.st_size)
    with _MASKING_LOCK:
        cached = _MASKING_SUPPORT.get(cache_key)
    if cached is None:
        result = wg.run([binary, "--help"], check=False, timeout=5.0, dry_stdout="masking")
        text = f"{result.stdout}\n{result.stderr}".lower()
        cached = "masking" in text
        with _MASKING_LOCK:
            _MASKING_SUPPORT[cache_key] = cached
    if not cached:
        raise ObfmeshError(
            f"{binary} не знает параметр masking: маскировка {settings.masking.value} "
            "не будет применена, а без неё провайдер режет поток; обновите "
            "wg-obfuscator до 1.6+ либо снимите проверку OBFMESH_SKIP_MASKING_CHECK=1"
        )


def _write_file_if_changed(path: str, content: str, mode: int) -> bool:
    """Атомарная запись при изменении содержимого. Возвращает True, если писали."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            current = handle.read()
    except FileNotFoundError:
        current = None
    if current == content:
        if (os.stat(path).st_mode & 0o777) != mode:
            os.chmod(path, mode)
        return False

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".obfmesh-", suffix=".tmp")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        _remove_file(tmp_path)
        raise
    return True


def _read_port_list(path: str) -> set[int]:
    return {int(item) for item in _read_line_list(path) if item.isdigit()}


def _write_port_list(path: str, ports: set[int]) -> None:
    _write_line_list(path, {str(port) for port in ports})


def _read_line_list(path: str) -> set[str]:
    try:
        with open(path, "r", encoding="ascii") as handle:
            return {line.strip() for line in handle if line.strip()}
    except OSError:
        return set()


def _write_line_list(path: str, values: set[str]) -> None:
    content = "".join(f"{value}\n" for value in sorted(values))
    try:
        _write_file_if_changed(path, content, 0o600)
    except OSError as exc:
        log.warning("не удалось записать %s: %s", path, exc)


def _write_pidfile(path: str, pid: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(f"{pid}\n")


def _read_pidfile(path: str) -> int | None:
    try:
        with open(path, "r", encoding="ascii") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def _read_cmdline(pid: int) -> list[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        return []
    return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]


def _reap(pid: int) -> None:
    """Снять зомби, если процесс — наш ребёнок; для чужого pid это no-op.

    Подобранный по pid-файлу процесс обычно чужой, но после `_procs.clear()`
    (или потери Popen) он остаётся нашим ребёнком: убитый и не подобранный,
    он висел бы зомби, а `kill(pid, 0)` продолжал бы говорить «жив».
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # kill(pid, 0) успешен и для зомби. На Linux состояние читается из /proc,
    # где зомби отличим; на других системах остаётся ответ kill().
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rpartition(b")")[2].split()
        if fields and fields[0] == b"Z":
            return False
    except OSError:
        pass
    return True


def _remove_file(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("не удалось удалить %s: %s", path, exc)


# --- одиночка ---------------------------------------------------------------

_instance: Orchestrator | None = None
_instance_lock = threading.Lock()


def get_orchestrator() -> Orchestrator:
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = Orchestrator()
        return _instance


def reset_orchestrator() -> None:
    """Сбросить одиночку — нужно тестам."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop_supervisor()
        _instance = None


def reconcile() -> ReconcileReport:
    """Привести систему к желаемому состоянию (SPEC: вызывается после любого изменения)."""
    return get_orchestrator().reconcile()


def status() -> dict[str, Any]:
    return get_orchestrator().status()
