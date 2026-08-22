# Упаковка obfmesh

Как из исходников получить пакеты, которые ставятся одной командой и снимаются другой:
`.apk`/`.ipk` для роутера и `.deb` для сервера. Установка собранного описана в
[INSTALL.md](INSTALL.md), контракт компонентов — в [SPEC.md](SPEC.md).

Текущая версия релиза — **1.2.0**. Она записана в четырёх местах, и все четыре поднимаются одной
правкой (раздел 4.1).

## 1. Что во что упаковывается

| Артефакт | Что внутри | Архитектура | Чем собирается |
|---|---|---|---|
| `obfmesh` | POSIX-shell клиент: init.d, apply.sh, watcher.sh, tune.sh, lib.sh, CLI, uci-конфиг | `all` (noarch) | OpenWrt SDK |
| `luci-app-obfmesh` | страница статуса в LuCI, ACL, пункт меню | `all` (noarch) | OpenWrt SDK + feed luci |
| `obfmesh-server` | Python-сервис, systemd-юнит, logrotate, `obfmesh-ctl` | `all` | `server/Makefile` + `dpkg-deb` |
| `wg-obfuscator` | не наш проект | нативная | собирается отдельно, в пакеты не входит |

Оба клиентских пакета не содержат ни одного скомпилированного файла, поэтому `PKGARCH:=all`
(в apk это `arch: noarch`). Из этого следует полезное: собранный пакет годится для любого
устройства с OpenWrt 25.12, независимо от процессора и версии ядра. Ядро важно только для
зависимостей `kmod-*`, которые пакет не несёт, а требует.

---

## 2. Клиент под OpenWrt

### 2.1 Какой SDK брать

Целевая прошивка — FriendlyWrt 25.12.5 на NanoPi R3S: это OpenWrt 25.12.5 (ветка `openwrt-25.12`,
тег `v25.12.5`) с ядром 6.1.141 из BSP FriendlyElec.

**Официальный SDK 25.12.5 закрывает сборку целиком.** Пакеты noarch, от ядра не зависят, а важна
только версия сборочной системы: 25.12 — это `apk`, формат имён файлов, `ALTERNATIVES` у `ip-full`
и текущие `default_postinst`/`default_prerm`.

```
https://downloads.openwrt.org/releases/25.12.5/targets/rockchip/armv8/
    openwrt-sdk-25.12.5-rockchip-armv8_gcc-14.3.0_musl.Linux-x86_64.tar.zst
```

Отдельных `kmod-*` собирать не нужно. До 1.2.0 пакет тянул `kmod-vrf` и `kmod-sched-teql`, и это
была главная сложность сборки: модули из официального SDK собраны под ядро OpenWrt, а на устройстве
ядро FriendlyElec 6.1.141, vermagic не совпадает. В 1.2.0 общих устройств нет — ни VRF, ни teql, —
поэтому из модулей остался только `kmod-wireguard`, который есть в feed прошивки.

Проверка версии сборочной системы до сборки:

```sh
grep -E '^(CONFIG_VERSION_NUMBER|CONFIG_USE_APK)=' .config
```

`CONFIG_USE_APK=y` — на выходе будет `.apk`, иначе `.ipk`. Это решение всей сборки, а не наших
Makefile: они одинаково работают в обоих режимах.

### 2.2 Подготовка SDK

```sh
tar --zstd -xf openwrt-sdk-25.12.5-rockchip-armv8_gcc-14.3.0_musl.Linux-x86_64.tar.zst
cd openwrt-sdk-25.12.5-rockchip-armv8_gcc-14.3.0_musl.Linux-x86_64

./scripts/feeds update -a
./scripts/feeds install luci-base
```

Feed `luci` обязателен даже если LuCI-страница не нужна: `luci-app-obfmesh/Makefile` подключает
`feeds/luci/luci.mk`, а сборка тянет хост-инструменты из `luci-base/host` (минификация JS,
`po2lmo`).

### 2.3 Куда положить исходники

Способ А, разовая сборка — прямо в дерево:

```sh
cp -r /path/to/obfmesh/openwrt/obfmesh          package/obfmesh
cp -r /path/to/obfmesh/luci/luci-app-obfmesh    package/luci-app-obfmesh
```

Артефакты появятся в `bin/targets/rockchip/armv8/packages/`.

Способ Б, повторяемая сборка — собственный feed. Так пакеты живут рядом с остальными и получают
свой каталог в репозитории:

```sh
cat >> feeds.conf <<'EOF'
src-link obfmesh /path/to/obfmesh/feed
EOF

./scripts/feeds update obfmesh
./scripts/feeds install -a -p obfmesh
```

`feeds.conf` ждёт каталог, в котором каждый подкаталог — пакет, поэтому дерево репозитория
раскладывается так:

```
feed/
  obfmesh/            -> симлинк или копия openwrt/obfmesh
  luci-app-obfmesh/   -> симлинк или копия luci/luci-app-obfmesh
```

Артефакты в этом случае попадут в `bin/packages/aarch64_generic/obfmesh/`. При сборке из feed
`luci-app-obfmesh/Makefile` продолжает работать: строка `include $(TOPDIR)/feeds/luci/luci.mk`
верна и там. Менять её на `../../luci.mk` нужно, только если каталог кладётся внутрь
`feeds/luci/applications/`.

### 2.4 Сборка

```sh
make defconfig
echo 'CONFIG_PACKAGE_obfmesh=m'           >> .config
echo 'CONFIG_PACKAGE_luci-app-obfmesh=m'  >> .config
make defconfig

make package/obfmesh/compile V=s
make package/luci-app-obfmesh/compile V=s

find bin -name 'obfmesh-*' -o -name 'luci-app-obfmesh-*'
```

Ожидаемые имена: `obfmesh-1.2.0-r1.apk` и `luci-app-obfmesh-1.2.0-r1.apk` (`.apk` — это
`<имя>-<PKG_VERSION>-r<PKG_RELEASE>.apk`; для ipk то же самое выглядит как
`obfmesh_1.2.0-r1_all.ipk`).

Опций сборки у пакета нет. Символ `CONFIG_PACKAGE_obfmesh_teql`, который в 1.1.0 подтягивал
`kmod-sched-teql`, удалён вместе с режимом `teql`: он не работал в принципе (SPEC, «Что проверено и
отвергнуто», п. 1). Если в `.config` осталась строка `CONFIG_PACKAGE_obfmesh_teql=y` от прежних
сборок, `make defconfig` её выбросит.

### 2.5 conffiles: не забыть и проверить

`/etc/config/obfmesh` содержит клиентский токен. Он **обязан** быть объявлен в
`Package/obfmesh/conffiles` — это не гигиена, а условие работоспособности обновления:

```
define Package/obfmesh/conffiles
/etc/config/obfmesh
endef
```

Что случилось без этого на бою: конфиг обновили копированием файлов мимо пакетного менеджера, файл
из сборки лёг поверх рабочего, токен пропал — и клиент потерял сервер. Симптом при этом
неочевидный: сервис жив, watcher работает, в логе 401, лучи стоят.

Отсюда два правила:

1. Обновляться только пакетом (`apk add ./obfmesh-1.2.0-r1.apk`) или через `install-nosdk.sh`,
   который существующий `/etc/config/obfmesh` не трогает. Ручной `cp -r files/* /` — способ
   потерять токен, и он не делается никогда.
2. После каждой сборки проверять, что объявление на месте, — на устройстве, обновлением поверх:

```sh
md5sum /etc/config/obfmesh
apk add --allow-untrusted ./obfmesh-1.2.0-r1.apk
md5sum /etc/config/obfmesh      # хеш обязан совпасть со снятым до обновления
ls /etc/config/obfmesh*         # новый образец лёг рядом как obfmesh.apk-new
grep -c token /etc/config/obfmesh
```

Для ipk то же самое видно прямо в пакете, без установки:

```sh
tar -xzOf obfmesh_1.2.0-r1_all.ipk ./control.tar.gz | tar -xzO ./conffiles
```

### 2.6 Что ещё проверить в собранном пакете

На сборочной машине — что список файлов и права те, что задумывались (для ipk):

```sh
tar -xzOf bin/.../obfmesh_1.2.0-r1_all.ipk ./data.tar.gz | tar -tvz
tar -xzOf bin/.../obfmesh_1.2.0-r1_all.ipk ./control.tar.gz | tar -xzO ./control
```

Для apk удобнее проверять на устройстве, до установки:

```sh
apk add --allow-untrusted --simulate ./obfmesh-1.2.0-r1.apk
```

`apk add` принимает путь к файлу наравне с именем пакета, а `--simulate` показывает, что было бы
установлено и какие зависимости не разрешились, ничего не меняя.

После установки:

```sh
apk info -L obfmesh              # список файлов
apk info -R obfmesh              # зависимости
apk manifest obfmesh             # файлы с контрольными суммами
ls -ld /etc/obfmesh              # 700
ls -l /etc/config/obfmesh        # 600, в нём токен
ls -l /usr/lib/obfmesh/          # apply.sh, watcher.sh, tune.sh исполняемые, lib.sh 644
```

Что должно быть верно и почему:

- `/etc/config/obfmesh` ставится через `$(INSTALL_CONF)`, то есть 0600: в нём клиентский токен.
- `/etc/obfmesh` — каталог с правами 700, в нём окажется `bundle.json` с приватными ключами.
- `lib.sh` ставится как данные (0644): он подключается через `.`, а не запускается.
- `tune.sh` ставится исполняемым: его запускают init.d и apply.sh. До 1.2.0 этого файла в пакете
  не было, и настройка роутера жила ручным костылём поверх пакета — при обновлении она терялась
  молча, а вместе с ней 150 Мбит/с из 359.
- Зависимости — `wireguard-tools`, `kmod-wireguard`, `curl`, `jsonfilter`, `ip-full`, и всё.
  `kmod-vrf`, `tc-full` и `kmod-sched-teql` из списка убраны: устройств, ради которых они стояли,
  больше нет. `apk info -R obfmesh` не должен их показывать.
  Единственное, что ещё умел бы `tc`, — снять teql-дисциплину с луча на роутере, обновляемом с
  1.1.0. Обнаруживает её obfmesh не через `tc` (без него `tc qdisc show` молчит, и луч оставался
  бы мёртвым незаметно), а через `ip link`. Если `tc` нет, устройство луча удаляется и тот же
  apply создаёт его заново — путь рабочий, просто шумнее в логе.
- В пакете нет `prerm`. Стоп и disable делает `default_prerm()` из `/lib/functions.sh`, причём
  `disable` — только при настоящем удалении. Собственный `prerm`, который снимал бы `enable`
  всегда, оставлял бы сервис после обновления запущенным, но выключенным: до первой перезагрузки
  никто бы этого не заметил.
- `postrm` срабатывает только при удалении (`PKG_UPGRADE` не выставлен) и уносит `bundle.json`
  вместе с промежуточными файлами `/etc/obfmesh/.bundle.*` и со всем `/var/run/obfmesh`, где
  лежит `curl.<pid>.rc` с токеном. Имя `bundle.json.new` там тоже перечислено: так называл свой
  промежуточный файл релиз до 1.1.0, и после обновления он может остаться на флеше.
- `openwrt/obfmesh/tests/` в пакет не входит: `Package/obfmesh/install` перечисляет файлы
  поимённо, каталог с тестами и заглушками остаётся в исходниках.

### 2.7 Свой репозиторий для устройств

Ставить файлом (`apk add --allow-untrusted ./pkg.apk`) удобно на одном роутере. Дальше нужен
репозиторий: обновления приходят через `apk upgrade`, а `--allow-untrusted` перестаёт быть
обязательным.

```sh
make package/index
```

В каждом каталоге `bin/**/packages/` появляются `packages.adb` (индекс) и `index.json`. Индекс
подписывается ключом сборочного дерева, если включено `CONFIG_SIGNED_PACKAGES`; сама пара ключей
создаётся при первой сборке пакетов:

```
private-key.pem   # остаётся в сборочном дереве, наружу не отдаётся
public-key.pem    # раздаётся устройствам
```

Раздаём каталог по HTTPS и подключаем на роутере:

```sh
# доверие к нашему индексу: имя файла произвольное, apk читает все файлы каталога
scp public-key.pem root@192.168.2.1:/etc/apk/keys/obfmesh-local.pem

# ВНИМАНИЕ: не перезаписывайте /etc/apk/keys/public-key.pem — это ключ прошивки,
# без него роутер потеряет доверие к своему собственному feed.

cat >/etc/apk/repositories.d/obfmesh.list <<'EOF'
https://pkg.example.net/obfmesh/aarch64_generic/obfmesh/packages.adb
EOF

apk update
apk add obfmesh luci-app-obfmesh
```

Для сборки без apk (ipk) индекс называется `Packages`/`Packages.gz`, подписывается `usign`, а на
устройстве прописывается строкой `src/gz obfmesh https://...` в `/etc/opkg/customfeeds.conf`.

Отдельный ключ вместо `--allow-untrusted` стоит завести, как только пакет ставится больше чем на
один роутер: иначе привычка ставить неподписанное расползается на все пакеты.

### 2.8 Чем отличается сборка luci-app

`luci.mk` делает почти всё сам, поэтому Makefile страницы такой короткий:

- `PKG_NAME` берётся из имени каталога (`luci-app-obfmesh`);
- `Package/<name>`, `Build/Prepare`, `Build/Compile`, `Package/<name>/install` и финальный
  `BuildPackage` описаны в самом `luci.mk`;
- `htdocs/` уезжает в `/www`, `root/` — в корень файловой системы;
- JS минифицируется, если включено `CONFIG_LUCI_JSMIN` (по умолчанию да);
- при удалении/установке дёргается `rpcd reload` и чистится кеш LuCI — это дефолтный `postinst`
  из `luci.mk`, свой писать не нужно.

Что задано у нас руками и зачем:

| Переменная | Зачем |
|---|---|
| `PKG_VERSION`, `PKG_RELEASE` | вне feed luci версия не выводится из git, её надо задать |
| `PKG_LICENSE` | иначе поле `license` в пакете пустое |
| `LUCI_DEPENDS:=+obfmesh +luci-base` | страница дёргает `/usr/bin/obfmesh` через `rpcd-mod-file`, который приходит с `luci-base` |
| `LUCI_MAINTAINER` | по умолчанию `luci.mk` подписывает пакет сообществом LuCI |
| `LUCI_PKGARCH:=all` | и так `all` для страницы без `src/`, но лучше явно |

Список разрешённых команд лежит в `root/usr/share/rpcd/acl.d/luci-app-obfmesh.json` и ограничен
конкретными строками (`/usr/bin/obfmesh status --json`, `... spokes *` и так далее). Расширять его
следует ровно на ту команду, которая понадобилась: `file.exec` в rpcd — это выполнение от root.
Строка `/usr/bin/obfmesh agg-mode *` из ACL убрана: страница эту команду больше не вызывает, а
сама она ничего не делает (печатает объяснение и выходит с кодом 2). Вместо неё разрешён
`/usr/bin/obfmesh tune` — кнопка «Re-tune» на странице.

Переводов сейчас нет. Появятся — каталог `po/ru/*.po` соберётся в отдельный пакет
`luci-i18n-obfmesh-ru` автоматически, без правки Makefile.

---

## 3. Сервер: пакет или контейнер

### 3.1 Сравнение

| | `.deb` | Контейнер |
|---|---|---|
| Интерфейсы `swg{i}` | создаются на хосте как есть | нужен `--network host`, иначе интерфейсы окажутся в чужом netns |
| `net.ipv4.ip_forward=1` | пишется напрямую | `/proc/sys` в контейнере read-only: нужен `--privileged` либо проброс sysctl |
| Правила NAT и FORWARD | в таблицах хоста | нужен `NET_ADMIN` и совпадающий backend iptables (nft/legacy) с хостовым |
| Обфускаторы переживают рестарт | да: `KillMode=process`, подбор по pid-файлам | нет: процессы — дети контейнера и умирают вместе с ним, если не `--pid host` |
| Каталоги состояния | `StateDirectory`, `RuntimeDirectoryPreserve` в юните | тома, права и `umask` настраиваются руками |
| Логи | journald + logrotate | ещё один сборщик логов |
| Обновление | `apt-get install ./pkg.deb` | пересборка образа |
| Изоляция | никакой | почти никакой: чтобы всё работало, нужны host network, host pid и NET_ADMIN |

Вывод: **правильный путь — `.deb`**. obfmesh не приложение в песочнице, а управляющий слой над
сетевым стеком хоста: он создаёт интерфейсы, правит sysctl, ведёт правила iptables и присматривает
за процессами. Контейнер, которому всё это разрешено, отличается от установки на хост только тем,
что мешает systemd делать его работу и ломает специально заложенное свойство «перезапуск
управляющего сервиса не рвёт туннели».

Где контейнер уместен: стенд для API и панели без реальной сети. Для этого в коде есть
`OBFMESH_DRY_RUN=1` — команды `ip`/`wg`/`iptables` только логируются. Такой контейнер не требует
ни привилегий, ни host network, и годится для демонстрации интерфейса и прогона тестов:

```sh
podman run --rm -p 127.0.0.1:8080:8080 \
    -e OBFMESH_DRY_RUN=1 -e OBFMESH_ADMIN_KEY=dev-key-not-for-production \
    -e OBFMESH_EXTERNAL_HOST=203.0.113.10 \
    -v obfmesh-db:/var/lib/obfmesh \
    obfmesh-server:dev
```

Отдавать такой контейнер в бой нельзя: в dry-run он рапортует об успехе, ничего не сделав.

### 3.2 Сборка .deb

```sh
cd server
make deb
```

На выходе — `dist/obfmesh-server_1.2.0-1_all.deb`. Требуется только `dpkg-deb` (пакет `dpkg`,
который есть на любой Debian/Ubuntu) и `make`; ни debhelper, ни dh-python не нужны, поэтому пакет
собирается прямо на сервере из копии репозитория. На машине без dpkg — в контейнере:

```sh
tar -cf - --exclude build --exclude dist . |
    podman run --rm -i ubuntu:24.04 sh -c \
    'mkdir /src && tar -C /src -xf - && cd /src && apt-get update -qq &&
     apt-get install -y -qq make && make deb && cat dist/*.deb' > obfmesh-server.deb
```

Цели `server/Makefile`:

| Цель | Что делает |
|---|---|
| `make deb` | собрать пакет |
| `make wheels` | скачать Python-зависимости колёсами в `vendor/wheels` для офлайн-установки |
| `make test` | создать временный venv и прогнать `pytest tests` |
| `make lint` | `sh -n` по maintainer-скриптам и `bash -n` по `install.sh` |
| `make clean` / `distclean` | убрать `build/`, `dist/` (и `vendor/wheels`) |

Версия пакета собирается из одного места — `__version__` в `server/obfmesh/__init__.py` — плюс
ревизия упаковки: `make deb DEB_REVISION=2` даст `1.2.0-2`. Ревизия поднимается, когда меняется
только упаковка (юнит, зависимости, maintainer-скрипты), а код сервера тот же.

### 3.3 Что кладёт пакет

| Путь | Что |
|---|---|
| `/opt/obfmesh/obfmesh/*.py`, `static/*` | код и панель |
| `/opt/obfmesh/requirements.txt` | список зависимостей, по его хешу postinst решает, надо ли трогать venv |
| `/opt/obfmesh/wheels/` | только если пакет собран после `make wheels` |
| `/usr/lib/systemd/system/obfmesh-server.service` | юнит |
| `/etc/logrotate.d/obfmesh` | ротация логов обфускаторов, единственный conffile пакета |
| `/usr/sbin/obfmesh-ctl` | локальное управление без HTTP |

Каталог кода — `/opt/obfmesh`, потому что путь зашит в юните (`WorkingDirectory`,
`ExecStart=/opt/obfmesh/.venv/bin/python`, `OBFMESH_STATIC_DIR`). Переезд в `/usr/lib/obfmesh`
потребовал бы править юнит, то есть менять код ради опрятности — не сделано сознательно.

Секретов в пакете нет и быть не может: админ-ключ генерирует postinst, ключи лучей и клиентов
живут в базе, ключ обфускации — в `/etc/obfmesh/obf{i}.conf` (600).

Maintainer-скрипты делают ровно то, чего dpkg сделать не может:

- **postinst**: каталоги 700 (`/etc/obfmesh`, `/var/lib/obfmesh`, `/var/log/obfmesh`), генерация
  админ-ключа (значение не печатается), создание venv и установка зависимостей, drop-in с
  `OBFMESH_EXTERNAL_HOST`/`OBFMESH_OBFUSCATOR_BIN`, если они были в окружении, `caddy-snippet.txt`,
  `enable --now` при первой установке и `try-restart` при обновлении. Плюс предупреждения:
  нет обфускатора, нет слушателя на :443, в `/etc/systemd/system` лежит юнит от `install.sh` и
  перекрывает пакетный.
- **prerm**: при удалении — `systemctl stop`, затем `obfmesh-ctl teardown --interfaces`. Иначе
  обфускаторы, которые специально переживают остановку сервиса, остались бы висеть без кода,
  которым их можно погасить. Правила NAT/FORWARD не снимаются: они общие, и удаление одного
  экземпляра не должно ломать соседний — команды печатаются в вывод.
- **postrm**: при `remove` уносит venv и `__pycache__` (их создаёт `obfmesh-ctl`, dpkg о них не
  знает), при `purge` — базу, логи и `/etc/obfmesh` с ключами.

Зависимости пакета: `python3 (>= 3.11), python3-venv, iproute2, iptables, wireguard-tools,
logrotate, systemd`. Обратный прокси стоит в `Suggests`, а не в `Depends` или `Recommends`:
он обязателен для работы с роутером, но apt не должен по своей инициативе устанавливать веб-сервер
и переписывать HTTP-настройку машины. За то, что прокси не забыт, отвечает предупреждение
postinst.

### 3.4 Установка без сети

По умолчанию postinst ставит зависимости в venv через pip из сети. Если сервер в сеть не ходит,
колёса вшиваются в пакет:

```sh
make wheels                 # цель по умолчанию: CPython 3.12, x86_64
make deb
```

Для другой машины:

```sh
make wheels WHEEL_MACHINE=aarch64
make wheels WHEEL_PYVERSION=3.11 WHEEL_ABI=cp311     # Debian 12
```

Скачиваются только бинарные колёса: исходный дистрибутив пришлось бы компилировать на целевой
машине, а это ровно то, чего офлайн-установка сделать не может. Список платформенных тегов
намеренно длиннее одного (`manylinux_2_28`, `manylinux_2_17`, `manylinux2014`): pip сравнивает
`--platform` с тегом колеса буквально, а закреплённые в `requirements.txt` версии `uvloop` и
`httptools` публикуются только под `manylinux_2_17`.

Проверка, что колёса попали в пакет и используются:

```sh
dpkg-deb -c dist/obfmesh-server_1.2.0-1_all.deb | grep wheels
# при установке в логе: "installing the python dependencies from the wheels vendored in the package"
```

### 3.5 install.sh и пакет — не вместе

`server/install.sh` остаётся способом установки без пакетного менеджера, и он самодостаточен:
копирует код в `/opt/obfmesh`, ставит юнит в `/etc/systemd/system`, создаёт `obfmesh-ctl` в
`/usr/local/sbin` и в конце проверяет, что каждый настроенный луч поднят.

На одной машине выбирается что-то одно. Признак смешения — юнит в `/etc/systemd/system`,
перекрывающий пакетный из `/usr/lib/systemd/system`; postinst это замечает и говорит вслух. Чтобы
перейти с `install.sh` на пакет:

```sh
systemctl disable --now obfmesh-server
rm -f /etc/systemd/system/obfmesh-server.service /usr/local/sbin/obfmesh-ctl
systemctl daemon-reload
apt-get install -y ./obfmesh-server_1.2.0-1_all.deb
```

База `/var/lib/obfmesh/obfmesh.db`, ключи в `/etc/obfmesh` и работающие обфускаторы при этом
переживают переход: пакет подхватит и то, и другое.

---

## 4. Версионирование и совместимость

### 4.1 Где лежит версия

| Файл | Переменная | Значение в этом релизе | Куда попадает |
|---|---|---|---|
| `server/obfmesh/__init__.py` | `__version__` | `1.2.0` | `GET /api/status`, `obfmesh-ctl --version`, версия `.deb` |
| `openwrt/obfmesh/Makefile` | `PKG_VERSION`, `PKG_RELEASE` | `1.2.0`, `1` | имя файла пакета, метаданные apk |
| `openwrt/obfmesh/files/usr/lib/obfmesh/lib.sh` | `OM_VERSION` | `1.2.0` | `obfmesh status`, `obfmesh version`, заголовок `User-Agent` |
| `luci/luci-app-obfmesh/Makefile` | `PKG_VERSION`, `PKG_RELEASE` | `1.2.0`, `1` | имя файла пакета |

Правило простое: у релиза одна версия `X.Y.Z` на все четыре места, поднимаются они одной правкой.
`PKG_RELEASE` (и `DEB_REVISION` у сервера) поднимается отдельно, когда меняется только упаковка.

Расходятся эти четыре значения молча — ничто в коде их не сверяет. Поэтому смена версии — часть
релиза, а не побочный эффект правки. Проверка перед выпуском занимает секунду:

```sh
grep -rn 'PKG_VERSION\|OM_VERSION\|__version__' \
     openwrt/obfmesh/Makefile luci/luci-app-obfmesh/Makefile \
     openwrt/obfmesh/files/usr/lib/obfmesh/lib.sh server/obfmesh/__init__.py
grep -rn '1\.1\.0' --include=Makefile --include='*.sh' --include='*.py' --include='*.md' .
```

Вторая команда должна оставлять только исторические упоминания в CHANGELOG.md.

### 4.2 Что считается ломающим изменением

Совместимость определяется одним: разберёт ли клиент бандл. Разбор — `jsonfilter` по фиксированным
путям плюс проверка `om_bundle_validate()` в `lib.sh`.

Клиент 1.2.0 **требует** и отвергает бандл целиком, если чего-то нет:

- `config_version` — число;
- `server.host` — непустое;
- `server.mtu` — число;
- `obfuscation_key` — непустое;
- хотя бы один элемент `spokes[]` с `index` в диапазоне 1..10 и полным ключевым материалом.

Клиент **терпит** отсутствие: `spokes[].local_port` и `spokes[].server_port` (подставит `13300+i`
и `48200+i`), `server.masking` (подставит `STUN`).

Клиент **пропускает отдельный луч** без `wg_private_key`, `wg_server_pubkey`, `address` или
`peer_address` — с предупреждением в лог, остальные лучи поднимаются (инвариант 5 SPEC).

Клиент **игнорирует** любые незнакомые поля: он читает по путям, а не разбирает объект целиком.
Поля `agg_mode` и `agg_address`, которые сервер 1.1.0 клал в бандл, попадают именно в эту
категорию: клиент 1.2.0 их не читает и не проверяет.

Отсюда:

| Изменение бандла | Старый клиент, новый сервер |
|---|---|
| Добавили новое поле | безопасно, поле не читается |
| Добавили луч (увеличили `spokes`) | безопасно |
| Изменили `port_base`, адресацию, MTU | безопасно: значения приходят в бандле |
| Переименовали или убрали обязательное поле | **ломает** |
| Сменили тип поля (число → строка) | **ломает** для `config_version` и `mtu` |
| **Убрали `agg_mode` и `agg_address` (сделано в 1.2.0)** | **ломает клиента 1.1.0**: у него `agg_mode` в списке обязательных и проверяется белым списком из `single`, `ecmp`, `teql` |

Отвергнутый бандл не оставляет клиента без связи: `om_sync_bundle` сообщает об ошибке и **не
перезаписывает** сохранённый бандл, лучи продолжают работать на предыдущей конфигурации. Но
изменения с сервера перестают доезжать, и заметить это можно только по логу.

### 4.3 Как выкатывать

Аддитивные изменения (первые три строки таблицы) — сервер первым, клиенты когда угодно.

Ломающие — наоборот: сначала клиенты во всём парке учатся понимать новый формат, и только потом
сервер начинает его отдавать. Между этими двумя моментами сервер обязан отдавать старый формат.

Переход 1.1.0 → 1.2.0 — ровно такой случай, и парк тут из одного роутера, поэтому порядок выката
простой и жёсткий:

1. Обновить клиента до 1.2.0 (он читает и старый бандл: лишние поля игнорируются).
2. Обновить сервер до 1.2.0 — с этого момента `agg_mode` и `agg_address` из бандла исчезают.
3. Убедиться, что бандл принят: `obfmesh status` показывает свежий `config_version`.

Клиент 1.1.0 против сервера 1.2.0 работать не будет: он отвергнет бандл целиком и останется на
последней принятой конфигурации, то есть на схеме с `agg0`, которой на сервере уже нет. Поэтому
откат на 1.1.0 делается только с обеих сторон сразу (DEPLOY.md, шаг 6).

Проверка после выката, на роутере:

```sh
obfmesh version                 # 1.2.0 и config_version применённого бандла
obfmesh status | head -20       # applied N s ago — бандл принят, а не отвергнут
logread -e obfmesh | grep -i 'rejected the downloaded bundle'   # пусто
```

### 4.4 Чего в механизме совместимости сейчас нет

Названо явно, чтобы не считалось реализованным:

1. **В бандле нет номера схемы.** `config_version` — это счётчик изменений состояния, он растёт от
   любой правки настроек и о формате не говорит ничего. Сверять версии формата нечем — ровно
   поэтому переход 1.1.0 → 1.2.0 требует ручного порядка выката из 4.3.
2. **Сервер не знает версию клиента.** Клиент шлёт `User-Agent: obfmesh/<версия>`, сервер этот
   заголовок не читает и отдаёт всем один и тот же бандл. Выдавать разным версиям разный формат
   сейчас нельзя.
3. **Клиент не использует ETag.** Сервер считает и отдаёт `ETag` и умеет отвечать `304` на
   `If-None-Match`, но клиент этот заголовок не шлёт: он скачивает бандл целиком и сравнивает md5
   локально. Работает верно, просто трафик не экономится.
4. **Нет проверки совместимости при установке.** Пакеты `obfmesh` и `luci-app-obfmesh` связаны
   зависимостью, а связи «клиент 1.2 требует сервер ≥1.2» не существует и существовать не может:
   это разные машины и разные пакетные менеджеры.

Что стоит сделать, если формат начнёт меняться дальше: добавить в бандл `bundle_schema` (целое,
растёт только при ломающем изменении), научить `om_bundle_validate` сравнивать его с максимальной
поддерживаемой схемой клиента и отвергать бандл с внятной строкой в логе, а на сервере — отдавать
формат по схеме, запрошенной клиентом. До тех пор действует правило из 4.3.
