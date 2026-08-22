# Установка obfmesh с нуля

Установка состоит из двух половин: управляющий сервер на Linux и клиент на роутере OpenWrt.
Порядок жёсткий — сначала сервер целиком, потом роутер: клиенту неоткуда взять бандл, пока
сервер не отвечает.

Что читать рядом: [SPEC.md](SPEC.md) — контракт, любое расхождение с ним ошибка;
[DEPLOY.md](DEPLOY.md) — тот же выкат, но пошагово с откатом на каждом шаге, для боевых машин
`45.136.127.10` и `192.168.2.1`; [PACKAGING.md](PACKAGING.md) — как собрать пакеты, которые тут
устанавливаются.

Обозначения те же, что в DEPLOY.md: `[S]` — блок команд выполняется на сервере под root,
`[R]` — на роутере под root.

Ни один шаг на роутере не трогает главную таблицу маршрутизации, firewall и конфигурацию
существующих обходных средств. obfmesh создаёт свои интерфейсы `owg{i}`, свои таблицы маршрутов
`51820+i` и свои правила с приоритетом `32000+i`. Пока потребители не привязаны к лучам
(шаг 3.7), для домашней сети установка незаметна.

Схема с версии 1.2.0 симметрична: **N независимых лучей**, у каждого свой адрес, своя таблица и
своё правило. Ни `agg0`, ни VRF, ни `teql`, ни агрегатного адреса больше нет — почему, написано в
SPEC.md, раздел «Что проверено и отвергнуто».

---

## 1. Что нужно и как проверить готовность

### 1.1 Сервер

| Что | Зачем | Минимум |
|---|---|---|
| Ubuntu 24.04 / Debian 12, root | systemd-юнит, управление сетевым стеком | — |
| Python | сам сервис | 3.11+ |
| `wireguard-tools`, `iproute2`, `iptables` | `wg`, `ip`, правила NAT | — |
| Модуль ядра `wireguard` | интерфейсы `swg{i}` | в составе ядра ≥5.6 |
| `wg-obfuscator` | обфускация | 1.6+, с поддержкой `masking` |
| Обратный прокси с TLS | публикация API наружу | Caddy или nginx |
| Внешние порты `udp/48201..4820N` | приём лучей | по числу лучей |
| Свободный `tcp/8080` на 127.0.0.1 | API | — |

Проверка `[S]`:

```sh
. /etc/os-release; echo "$PRETTY_NAME"
python3 -V
command -v wg ip iptables systemctl || echo 'ЧЕГО-ТО НЕТ'
modprobe wireguard && ls -d /sys/module/wireguard
ss -lntp | grep -E ':(443|8080)\b' || echo 'порты 443 и 8080 свободны'
```

Ожидается: Python 3.11 и выше, все четыре утилиты на месте, модуль загружается, порты свободны.
Если Python старше — дальше идти нельзя, сервис не запустится.

### 1.2 Роутер

| Что | Зачем |
|---|---|
| OpenWrt/FriendlyWrt 25.12.x, aarch64, procd, BusyBox ash, `apk` | целевая платформа |
| `wireguard-tools`, `kmod-wireguard` | интерфейсы `owg{i}` |
| `curl`, `jsonfilter` | REST, SSE, разбор бандла |
| `ip-full` | `ip rule` и `ip -d link` — в `ip-tiny` их нет |
| `wg-obfuscator` | той же версии, что на сервере |
| ~1 МБ свободной флеш-памяти | пакеты `obfmesh` и `luci-app-obfmesh` |

`kmod-vrf` и `kmod-sched-teql` в 1.2.0 не нужны: общих устройств не осталось, удалять их не
обязательно, но и держать незачем.

`tc-full` — отдельный случай. Зависимостью он больше не является: obfmesh ничего не вешает на
`tc`. Но роутер, обновляемый **с 1.1.0 в режиме `teql`**, приходит с teql-дисциплиной на лучах,
и снять её умеет только `tc`. Обнаруживает её obfmesh через `ip link` (он в зависимостях), а
если `tc` нет — удаляет устройство луча, и тот же apply создаёт его заново уже чистым. Оба пути
рабочие; с `tc` он тише. После первого удачного apply `tc-full` можно снимать.

`taskset` в зависимостях тоже нет, потому что это applet BusyBox на одних сборках и пакет
`util-linux-taskset` на других. Без него привязка обфускаторов к ядрам пропускается — а это
измеренные 359 → 210 Мбит/с, так что проверить стоит: `command -v taskset`.

Проверка `[R]`:

```sh
cat /etc/openwrt_release | head -3; uname -r
apk info -e wireguard-tools kmod-wireguard curl jsonfilter ip-full
ls -l /usr/bin/wg-obfuscator
ip route show table 51821; ip route show table 51822     # должно быть пусто
ip rule show | grep -E ' 3200[0-9]:' || echo 'правила свободны'
nproc; df -h /overlay | tail -1
```

Чего не хватает — ставится одной командой:

```sh
apk add ip-full
ip -V; readlink -f /sbin/ip     # ip-full приоритетнее ip-tiny в alternatives
```

`ip-full` и `ip-tiny` в 25.12 сосуществуют: `/sbin/ip` — симлинк, который система alternatives
переключает на `ip-full` (приоритет 300 против 200). Роутер не остаётся без `ip` ни на секунду.

Если таблицы `51821+` или приоритеты `32001+` уже заняты чужими правилами — задайте другие базы
на шаге 3.3 (`route_table_base`, `rule_pref_base`). Чужие маршруты obfmesh не трогает, но и делить
таблицу с кем-то не будет: уборка луча стирает таблицу целиком.

### 1.3 Обфускатор

`wg-obfuscator` нужен с обеих сторон и в пакеты не входит: путь задаётся настройкой
(`obfuscator_bin` в uci на роутере, `PATCH /api/settings` на сервере).

```sh
wg-obfuscator --version
wg-obfuscator --help | grep -i masking
```

В выводе `--help` обязано быть `masking`, среди значений — `STUN`. Без маскировки провайдер режет
поток, а выглядит это как «туннель поднят, рукопожатие есть, трафика нет». obfmesh проверяет это
сам и отказывается поднимать луч на бинарнике без `masking`.

Значение маскировки должно совпадать на обоих концах байт в байт — обе стороны пишут `STUN`
заглавными.

---

## 2. Сервер

### 2.1 Бинарник обфускатора

```sh
install -m 0755 wg-obfuscator /usr/local/bin/wg-obfuscator
/usr/local/bin/wg-obfuscator --help | grep -i masking
```

Путь произвольный, но его надо запомнить: он попадёт в настройки.

### 2.2 Управляющий сервис

Два способа, результат одинаковый. Пакет удобнее там, где нужны обновления и удаление одной
командой; `install.sh` — там, где сервер один и ставится из исходников.

**Способ А, пакет** (сборка описана в [PACKAGING.md](PACKAGING.md)):

```sh
OBFMESH_EXTERNAL_HOST=45.136.127.10 \
OBFMESH_OBFUSCATOR_BIN=/usr/local/bin/wg-obfuscator \
apt-get install -y ./obfmesh-server_1.2.0-1_all.deb
```

Обе переменные необязательны и читаются только при первом создании строки настроек. Без
`OBFMESH_EXTERNAL_HOST` адрес определяется по маршруту по умолчанию — на сервере с одним белым
адресом это обычно верно, но проверить стоит (`obfmesh-ctl status`). Изменить потом:
`PATCH /api/settings`.

Postinst создаёт venv, генерирует админ-ключ, включает и запускает юнит. Если Python-зависимости
поставить не удалось (нет сети и нет вшитых wheels), установка честно падает; после починки сети
— `apt-get -f install`.

**Способ Б, из исходников:**

```sh
apt-get install -y wireguard-tools iproute2 iptables python3-venv
cd server
OBFMESH_EXTERNAL_HOST=45.136.127.10 \
OBFMESH_OBFUSCATOR_BIN=/usr/local/bin/wg-obfuscator \
sudo -E ./install.sh
```

`install.sh` идемпотентен: повторный запуск при неизменных исходниках ничего не перезапускает. В
конце он требует, чтобы каждый настроенный луч был поднят, а его обфускатор жив, — то есть
проверяет не «FastAPI стартовал», а «система сошлась».

Смешивать способы не надо: `install.sh` кладёт юнит в `/etc/systemd/system`, а пакет — в
`/usr/lib/systemd/system`, и копия в `/etc` перекрывает пакетную. Postinst об этом предупреждает.

**Проверка** `[S]`:

```sh
systemctl is-active obfmesh-server
obfmesh-ctl status
wg show swg1 | head -5
ss -lunp | grep -E ':(48201|48202)\b'
pgrep -af wg-obfuscator
```

Ожидается: сервис `active`, два луча (значение по умолчанию), у каждого поднят интерфейс и жив
обфускатор, порты слушаются. Если нет — `journalctl -u obfmesh-server -n 80` и
`tail -50 /var/log/obfmesh/obf1.log`.

### 2.3 Что появилось в системе

| Путь | Что | Права |
|---|---|---|
| `/opt/obfmesh` | код и venv | 755 |
| `/etc/obfmesh/admin.key` | админ-ключ, значение нигде не печатается | 600 |
| `/etc/obfmesh/obf{i}.conf` | конфиги обфускаторов, в них ключ обфускации | 600 |
| `/etc/obfmesh/caddy-snippet.txt` | готовый блок для обратного прокси | 644 |
| `/var/lib/obfmesh/obfmesh.db` | настройки, ключи лучей, клиенты | 700 на каталоге |
| `/run/obfmesh/obf{i}.pid` | pid-файлы, по ним обфускаторы подбираются после рестарта | 700 на каталоге |
| `/var/log/obfmesh/obf{i}.log` | логи обфускаторов, ротация `/etc/logrotate.d/obfmesh` | 700 на каталоге |
| `obfmesh-ctl` | локальное управление без HTTP (`/usr/sbin` из пакета, `/usr/local/sbin` из install.sh) | 755 |

Интерфейсы `swg{i}` с адресом `10.77.{i}.1/24`, правила NAT для `10.77.0.0/16` и `ip_forward=1`
создаёт сам сервис при первом `reconcile()`. Никаких маршрутов к клиенту сервер не ставит: адрес
клиента в луче i лежит в `/24` своего `swg{i}` и попадает туда по AllowedIPs.

### 2.4 Публикация наружу

API слушает только `127.0.0.1:8080`. Это не настройка «по желанию»: без обратного прокси роутер до
сервера не достучится, а TLS взять неоткуда.

**Caddy.** Готовый блок лежит в `/etc/obfmesh/caddy-snippet.txt`:

```
45.136.127.10 {
    handle_path /obfmesh/* {
        reverse_proxy 127.0.0.1:8080 {
            flush_interval -1
        }
    }
}
```

`flush_interval -1` обязателен: `/api/events` — потоковый ответ, буферизация превращает
реактивность в опрос раз в 15 минут.

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

**nginx** — если он уже стоит на сервере:

```
location /obfmesh/ {
    proxy_pass http://127.0.0.1:8080/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 3600s;
}
```

Слэш в конце `proxy_pass` обязателен — он срезает префикс `/obfmesh`, как это делает `handle_path`
у Caddy. Панель строит адреса API относительно текущего пути, поэтому с обоими вариантами она
работает без дополнительных настроек. `proxy_buffering off` — то же самое требование, что
`flush_interval -1`.

**Порты обфускаторов.** `udp/48201..4820N` должны быть доступны снаружи. В юните стоит
`OBFMESH_MANAGE_INPUT=1`, и сервис сам вставляет разрешающие правила в INPUT; если правилами
управляете вы, поставьте `0` в drop-in и откройте порты сами.

**Проверка** `[S]`:

```sh
curl -sk -o /dev/null -w '%{http_code}\n' https://45.136.127.10/obfmesh/api/status
# 401 — прокси работает и аутентификация на месте

KEY=$(cat /etc/obfmesh/admin.key)
curl -sk -H "X-API-Key: $KEY" https://45.136.127.10/obfmesh/api/status | head -c 200
```

С третьей машины — что порт обфускатора виден снаружи:

```sh
nc -u -z -w3 45.136.127.10 48201; echo $?
```

и одновременно на сервере `tcpdump -ni any udp port 48201 -c 5`. Пакеты видны — путь есть.

**Сертификат для голого IP.** Caddy выписывает его своим внутренним CA, и curl на роутере ему не
поверит. Корневой сертификат понадобится на шаге 3.2:

```sh
cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt /root/obfmesh-ca.crt
```

### 2.5 Первый вход в панель

Откройте `https://45.136.127.10/obfmesh/`. Панель спросит админ-ключ — тот самый, что лежит в
`/etc/obfmesh/admin.key`:

```sh
sudo cat /etc/obfmesh/admin.key
```

Галочка «запомнить» кладёт ключ в `localStorage`, без неё — в `sessionStorage` на время вкладки. С
общей машины лучше без галочки.

Три раздела: **Состояние** (лучи, рукопожатия, счётчики), **Настройки лучей** (число лучей,
маскировка, MTU, внешний адрес), **Клиенты**.

### 2.6 Клиент и токен

Через панель: раздел «Клиенты» → имя (латиница, цифры, точка, дефис, подчёркивание) → «Создать».
Токен показывается **один раз**, вместе с готовой строкой `uci set ...` для роутера и кнопкой
скачивания бандла.

То же самое из консоли:

```sh
KEY=$(sudo cat /etc/obfmesh/admin.key)
curl -s -X POST http://127.0.0.1:8080/api/clients \
     -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"name":"nanopi"}'
```

Токен в открытом виде на сервере не хранится — только его хеш и первые восемь hex-символов
(`token_id`) для сверки «тот ли токен». Потеряли — заведите нового клиента.

Создание клиента сразу прописывает его пиров на все лучи, каждому — ровно один адрес:

```sh
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8080/api/clients
wg show swg1 allowed-ips        # 10.77.1.2/32 и ничего больше
wg show swg2 allowed-ips        # 10.77.2.2/32 и ничего больше
```

Второй адрес в AllowedIPs пира — след схемы 1.1.0 с агрегатным адресом. На 1.2.0 его быть не
должно; появился — это ошибка реализации, а не настройка.

---

## 3. Роутер

### 3.1 Пакеты

Сборка описана в [PACKAGING.md](PACKAGING.md). Локально собранные пакеты не подписаны индексом
репозитория, поэтому ставятся с явным `--allow-untrusted`:

```sh
apk add --allow-untrusted ./obfmesh-1.2.0-r1.apk ./luci-app-obfmesh-1.2.0-r1.apk
install -m 0755 wg-obfuscator /usr/bin/wg-obfuscator
/usr/bin/wg-obfuscator --help | grep -i masking
```

Оба пакета — одной командой: у `luci-app-obfmesh` зависимость на `obfmesh`.

Менеджер пакетов включает и запускает сервис сам. Пока не настроены `server_url` и `token`,
watcher только пишет об этом в лог — это ожидаемо.

Если на роутере лежал ручной костыль `/usr/lib/obfmesh/tune.sh` (настройка ядер и RPS поверх
пакета 1.1.0), уберите его вызов из `rc.local`/cron **до** установки: с 1.2.0 этот файл приходит с
пакетом и вызывается самим obfmesh.

**Проверка:**

```sh
ls -l /usr/lib/obfmesh/ /usr/bin/obfmesh      # apply.sh, watcher.sh, tune.sh, lib.sh
obfmesh version
ping -c2 8.8.8.8            # существующий выход в интернет не тронут
```

Обновление с 1.1.0 конфиг не трогает: `/etc/config/obfmesh` объявлен в `conffiles`, менеджер
пакетов кладёт рядом `.apk-new` и оставляет ваш файл с токеном на месте. Проверить, что так и
вышло:

```sh
grep -c token /etc/config/obfmesh      # токен на месте
ls /etc/config/obfmesh*                # obfmesh и, возможно, obfmesh.apk-new
```

### 3.2 Сертификат сервера

Нужен, только если сервер опубликован на голом IP с сертификатом внутреннего CA (шаг 2.4). Для
сертификата от публичного УЦ шаг пропускается: `curl` на OpenWrt использует
`/etc/ssl/certs/ca-certificates.crt`, который приходит вместе с `curl` (зависимость `ca-bundle`).

```sh
scp root@45.136.127.10:/root/obfmesh-ca.crt /etc/obfmesh/server-ca.crt
chmod 600 /etc/obfmesh/server-ca.crt
```

### 3.3 Настройка

```sh
uci set obfmesh.main.server_url='https://45.136.127.10/obfmesh'
uci set obfmesh.main.token='<токен из 2.6>'
uci set obfmesh.main.obfuscator_bin='/usr/bin/wg-obfuscator'
uci set obfmesh.main.ca_file='/etc/obfmesh/server-ca.crt'
uci commit obfmesh
```

Полный список опций с умолчаниями — в приложении В и в комментариях `/etc/config/obfmesh`.

Прежде чем запускать сервис, проверьте, что сервер вообще виден:

```sh
curl --cacert /etc/obfmesh/server-ca.crt -s -o /dev/null -w '%{http_code}\n' \
     https://45.136.127.10/obfmesh/api/status
# 401 — сервер отвечает и сертификат принят
```

`000` — не достучались (адрес, маршрут, firewall). `60` в тексте ошибки curl — сертификат не
принят: не тот CA-файл. Временный обход `uci set obfmesh.main.tls_insecure=1` открывает канал
управления для перехвата, поэтому годится только как способ диагностики.

### 3.4 Первый запуск

```sh
/etc/init.d/obfmesh restart
sleep 15
obfmesh status
```

Что должно быть в выводе:

- `config_version` не пустой, применён секунды назад;
- по каждому лучу `LINK up`, `OBFUSC ok:<pid>`, `HANDSHAKE` меньше двух минут, свой адрес
  `10.77.{i}.2/30`, своя таблица `51820+i`;
- `watcher: running`;
- строки `FAILED` нет;
- в полях `token` и `obfuscation key` — `<есть>`. Значений секретов в выводе быть не должно; если
  значение видно, это ошибка, а не особенность.

### 3.5 Проверка, что трафик ходит

Пока — не трогая домашнюю сеть, а привязываясь к лучу явно:

```sh
ping -c3 -I owg1 10.77.1.1                 # адрес сервера в первом луче
ping -c3 -I owg2 10.77.2.1

curl --interface owg1 -s https://ifconfig.me; echo
curl --interface owg2 -s https://ifconfig.me; echo
# оба должны ответить внешним адресом сервера

wg show owg1 transfer; wg show owg2 transfer
ip -4 addr show owg1                       # 10.77.1.2/30
ip route show table 51821                  # default dev owg1 mtu 1400
ip rule show | grep 5182                   # from 10.77.{i}.2 lookup 51820+i
```

Замер по одному лучу:

```sh
curl --interface owg1 -o /dev/null -s -w 'down %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin
```

Ожидаемый порядок для одного луча — около 180 Мбит/с (22 МБ/с). Суммарные 359–373 Мбит/с
получаются двумя такими замерами одновременно, каждый со своим `--interface`:

```sh
curl --interface owg1 -o /dev/null -s -w 'owg1 %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin &
curl --interface owg2 -o /dev/null -s -w 'owg2 %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin &
wait
```

Один поток по двум лучам не раскладывается — это свойство схемы, а не поломка (SPEC, «Что
проверено и отвергнуто», п. 4).

### 3.6 Настройка роутера

Без неё два луча дают 210 Мбит/с вместо 359–373. С 1.2.0 она приезжает с пакетом
(`/usr/lib/obfmesh/tune.sh`) и применяется идемпотентно при старте сервиса и после каждого
`apply` — pid обфускаторов меняется, привязку надо ставить заново.

Что делается:

1. Обфускаторы прибиваются к старшим ядрам, чтобы не драться за такты с сетевым softirq: на
   4-ядерном R3S с двумя лучами это CPU2 и CPU3 (`taskset -p 4 <pid1>`, `taskset -p 8 <pid2>`).
2. RPS на всех приёмных очередях физических интерфейсов:
   `echo f > /sys/class/net/<dev>/queues/rx-*/rps_cpus`.
3. `sysctl net.core.netdev_budget=600`.
4. Governor `performance` на всех ядрах.

Проверка `[R]`:

```sh
taskset -p "$(cat /var/run/obfmesh/obf-c1.pid)"    # current affinity mask: 4
taskset -p "$(cat /var/run/obfmesh/obf-c2.pid)"    # current affinity mask: 8
cat /sys/class/net/eth0/queues/rx-0/rps_cpus       # f
sysctl net.core.netdev_budget                      # 600
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # performance
```

Выключается настройкой `uci set obfmesh.main.tune=0` — например, если ядра распределяет другой
сервис. Тогда 359 Мбит/с ждать не стоит: измерения выше сняты с ней.

Числа рассчитаны на 4 ядра R3S: обфускаторы на старших ядрах, сетевой softirq на младших. На
другом железе их надо пересчитать под своё число ядер, иначе прирост не воспроизведётся.

### 3.7 Разведение сервисов по лучам

Первый шаг, который реально влияет на домашнюю сеть. Делайте, когда предыдущие пункты стабильно
отработали хотя бы сутки.

Именно здесь и получается агрегация. Ядро один поток по лучам не раскладывает, поэтому суммарная
скорость набирается тем, что **разные сервисы уходят в разные лучи**. Раскладка живёт у
потребителя, одной строкой на секцию.

Перед началом — снимок конфигурации:

```sh
cp /etc/config/network /etc/config/network.before-obfmesh
uci show forkop > /root/forkop.before-obfmesh 2>/dev/null
```

**Forkop.** У каждой секции своя опция `bind_interface`. Смотрим, что есть, и разводим:

```sh
uci show forkop | grep -E 'bind_interface|=.*section' | head -20

uci set forkop.<секция_1>.bind_interface='owg1'
uci set forkop.<секция_2>.bind_interface='owg1'
uci set forkop.<секция_3>.bind_interface='owg2'
uci set forkop.<секция_4>.bind_interface='owg2'
uci commit forkop
/etc/init.d/forkop restart
```

**sing-box.** То же самое полем исходящего:

```json
{
  "outbounds": [
    {"type": "direct", "tag": "via-owg1", "bind_interface": "owg1"},
    {"type": "direct", "tag": "via-owg2", "bind_interface": "owg2"}
  ]
}
```

**Что угодно ещё** — `SO_BINDTODEVICE` (`curl --interface owg1`) либо привязка к адресу
`10.77.{i}.2`: правило `from 10.77.{i}.2 lookup 51820+i` уведёт такой сокет в нужный луч.

Правила раскладки:

1. Один сервис — один луч. Размазывать один сервис по двум лучам смысла нет: его поток всё равно
   упрётся в 180 Мбит/с в том луче, куда попал.
2. Делить по нагрузке, а не по алфавиту: тяжёлое (видео, загрузки) — на один луч, разговорчивое и
   мелкое (мессенджеры, API) — на другой.
3. Число лучей и раскладка связаны вручную. Уменьшили число лучей на сервере — секции, привязанные
   к исчезнувшему интерфейсу, останутся без выхода, пока их не перевесят.

**Проверка:**

```sh
wg show owg1 transfer; wg show owg2 transfer     # растут оба, когда работают обе группы
curl -s https://ifconfig.me; echo                # с клиента домашней сети
obfmesh status
```

**Откат.** Вернуть потребителям прежний выходной интерфейс и перезапустить их. Раскладка обратима
целиком: obfmesh при этом не трогается.

---

## 4. Число обфускаторов

### 4.1 Сколько лучей

Число лучей — одна настройка на сервере, всё остальное подтягивается само: сервер поднимает
недостающие `swg{i}` и обфускаторы, повышает `config_version`, роутер получает событие по SSE,
скачивает новый бандл и меняет только разницу. Диапазон 1..10.

С роутера (нужен админ-ключ в uci):

```sh
uci set obfmesh.main.api_key='<админ-ключ сервера>'
uci commit obfmesh
obfmesh spokes            # прочитать
obfmesh spokes 3          # изменить
obfmesh status
```

С сервера:

```sh
KEY=$(sudo cat /etc/obfmesh/admin.key)
curl -s -X PATCH http://127.0.0.1:8080/api/settings \
     -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"spokes":3}'
```

Или в панели, раздел «Настройки лучей».

Уменьшение гасит только лишние лучи вместе с их таблицами и правилами: работающие не трогаются, их
ключи остаются в базе, поэтому обратное увеличение не меняет уже выданные бандлы. Установленные
сессии на оставшихся лучах не рвутся — маршруты этих лучей не меняются вообще.

**Сколько ставить.** На NanoPi R3S проверено замерами: два луча — 359–373 Мбит/с, три —
210–222 Мбит/с. Третий луч отнимает скорость, потому что процессор роутера уже загружен на
92–98 %. Увеличивать число лучей имеет смысл только на более сильном клиенте и только с замером до
и после.

### 4.2 Как убедиться, что луч действительно работает

«Интерфейс поднят» и «трафик идёт» — разные вещи. Каждый луч проверяется отдельно и полностью.

На роутере, для луча i (пример для первого):

```sh
ip -4 addr show owg1                   # 10.77.1.2/30, и этого адреса нет больше нигде
ip route show table 51821              # ровно одна строка: default dev owg1 mtu 1400
ip rule show | grep 51821              # 32001: from 10.77.1.2 lookup 51821
ping -c3 -I owg1 10.77.1.1
curl --interface owg1 -s https://ifconfig.me; echo
wg show owg1 transfer                  # оба счётчика растут
```

На сервере:

```sh
wg show swg1 allowed-ips               # 10.77.1.2/32 — один адрес, и только он
wg show swg2 allowed-ips               # 10.77.2.2/32
ip route show | grep 10.77.            # только подсети swg{i}, без nexthop-конструкций
```

Ключевой момент: адрес клиента принадлежит ровно одному лучу с обеих сторон. Второй адрес в
AllowedIPs, многопутевой маршрут к клиенту, устройство `agg0`, непустая таблица `51820` — это следы
схемы 1.1.0, а не рабочая конфигурация. После обновления с 1.1.0 стоит убедиться, что их не
осталось:

```sh
ip link show agg0 2>/dev/null && echo 'ОСТАТОК 1.1.0: agg0'
ip route show table 51820 | grep . && echo 'ОСТАТОК 1.1.0: таблица 51820'
ip rule show | grep -E ' 30000:' && echo 'ОСТАТОК 1.1.0: правило oif agg0'
ip -4 addr show | grep '10\.77\.0\.' && echo 'ОСТАТОК 1.1.0: агрегатный адрес'
```

**Реактивность.** На роутере:

```sh
obfmesh status | grep watcher          # running
logread -e obfmesh | tail -20          # ошибок чтения bundle.json быть не должно
```

Меняем число лучей на сервере (4.1) и через десяток секунд смотрим на роутере:

```sh
obfmesh status                         # config_version вырос сам, без ручного obfmesh sync
```

Если новое значение появляется только после ручного `obfmesh sync` — watcher до сервера не
доходит: смотрите `logread -e obfmesh` сразу после старта сервиса.

---

## 5. Диагностика

### 5.1 Куда смотреть

| Где | Команда или файл | Что там |
|---|---|---|
| сервер | `journalctl -u obfmesh-server -n 80` | API, reconcile, ошибки оркестратора |
| сервер | `/var/log/obfmesh/obf{i}.log` | вывод серверного обфускатора луча i |
| сервер | `obfmesh-ctl status` | лучи, интерфейсы, живость обфускаторов без HTTP |
| сервер | `obfmesh-ctl reconcile` | привести систему к состоянию из базы и показать, что менялось |
| роутер | `obfmesh status`, `obfmesh status --json` | сводка |
| роутер | `obfmesh logs -n 100`, `logread -e obfmesh` | apply.sh и watcher |
| роутер | `/var/run/obfmesh/obf-c{i}.log` | вывод клиентского обфускатора луча i |
| роутер | `/var/run/obfmesh/state.json` | что реально применено |
| роутер | `ip route show table 51820+i`, `ip rule show` | маршруты лучей |

Секреты в логи и в вывод команд не попадают: на их месте `<есть>`.

### 5.2 Симптом → куда смотреть

| Симптом | Что проверить |
|---|---|
| `obfmesh status` пуст, бандла нет | `obfmesh logs -n 50`; `server_url`, `token`, `ca_file`; `curl` из шага 3.3 |
| `GET /api/bundle` отвечает 401 | токен не тот или клиент удалён; заведите клиента заново |
| `GET /api/bundle` отвечает 409 | на сервере ещё нет лучей или не задан `external_host`: `obfmesh-ctl reconcile` |
| Луч поднят, рукопожатия нет | путь до `48200+i`: `tcpdump -ni any udp port 48201` на сервере; INPUT-правила |
| Обфускатор `DEAD` на роутере | `cat /var/run/obfmesh/obf-c1.log`; частая причина — занятый порт `13301` |
| Обфускатор падает на сервере | `tail -50 /var/log/obfmesh/obf1.log`, `journalctl -u obfmesh-server -n 80` |
| Рукопожатия идут, трафика нет | маскировка: `grep masking /etc/obfmesh/obf1.conf` и `/var/run/obfmesh/obf-c1.conf` — значения должны совпадать байт в байт |
| Через `owg{i}` не ходит ничего | проверка из 4.2 целиком: адрес, таблица, правило, AllowedIPs на сервере |
| Скорость как у одного луча | все потребители сидят на одном интерфейсе: `wg show owg2 transfer` не растёт — раскладка из 3.7 |
| Скорость около 210 вместо 359 Мбит/с | настройка роутера не применилась: проверка из 3.6 (`taskset`, `rps_cpus`, `netdev_budget`) |
| Скорость упала после добавления третьего луча | так и должно быть на R3S: 210–222 против 359–373, см. 4.1 |
| Один поток не разгоняется выше ~180 Мбит/с | это потолок одной TCP-сессии, лечения нет (SPEC, «Что проверено и отвергнуто») |
| Ответы не приходят на тот луч, с которого ушли | `wg show swg{i} allowed-ips` — там должен быть ровно один адрес клиента |
| Часть секций потребителя потеряла интернет | их луч погашен уменьшением `spokes`; перевесить `bind_interface` на живой |
| Изменения на сервере не подхватываются | строка `watcher` в `obfmesh status`; `logread -e obfmesh` |
| Сервис «останавливается» и встаёт обратно | `ls /var/run/obfmesh/admin-down` — watcher уважает этот маркер |
| После `systemctl stop` на сервере туннели живы | так задумано: `KillMode=process`. Гасить — `obfmesh-ctl teardown` |
| Панель отдаёт 502/504 на `/api/events` | буферизация прокси: `flush_interval -1` у Caddy, `proxy_buffering off` у nginx |
| После обновления пропал токен | конфиг перезаписан мимо пакетного менеджера; `/etc/config/obfmesh` обязан быть в `conffiles`, см. PACKAGING.md, 2.5 |

### 5.3 Ручные операции на роутере

```sh
obfmesh sync                # забрать бандл и применить, если изменился
obfmesh apply               # применить сохранённый бандл (снимает administrative down)
obfmesh down                # погасить всё, оставив сервис включённым
obfmesh logs -f             # хвост лога
obfmesh version             # версии пакета, бандла, обфускатора
```

`obfmesh down` ставит маркер `/var/run/obfmesh/admin-down`, иначе watcher поднял бы всё обратно на
ближайшем тике. Маркер живёт в tmpfs: после перезагрузки роутера туннель поднимется снова.

---

## 6. Удаление начисто

### 6.1 Роутер

```sh
# 1. Вернуть потребителям прежний выходной интерфейс (обратный шаг 3.7).
/etc/init.d/obfmesh stop
apk del luci-app-obfmesh obfmesh
```

`stop` гасит лучи, чистит таблицы `51820+i` и снимает правила. Удаление пакета дополнительно
стирает `/etc/obfmesh/bundle.json` (в нём приватные ключи) и весь `/var/run/obfmesh` (в нём
`curl.rc` с токеном).

Проверка, что не осталось следов:

```sh
ip link show owg1 2>/dev/null || echo 'лучи убраны'
ip route show table 51821                # пусто
ip rule show | grep -E ' 3200[0-9]:' || echo 'правил нет'
ls /etc/obfmesh /var/run/obfmesh 2>&1    # нет таких каталогов
pgrep -af wg-obfuscator || echo 'обфускаторов нет'
ping -c2 8.8.8.8                         # домашний интернет жив
```

Настройки, которые делал `tune.sh` (governor, RPS, `netdev_budget`), живут до перезагрузки и
никакого следа в конфигурации не оставляют. Нужно вернуть их немедленно — перезагрузите роутер или
верните значения руками.

Пакет `ip-full` ставился отдельно и остаётся: его могли использовать другие сервисы. Убирать —
только сознательно.

### 6.2 Сервер, установка пакетом

```sh
apt-get purge -y obfmesh-server
```

`prerm` останавливает сервис, гасит обфускаторы и удаляет интерфейсы `swg{i}`, `purge` стирает
`/etc/obfmesh` (админ-ключ, конфиги с ключом обфускации), `/var/lib/obfmesh` (база) и
`/var/log/obfmesh`. Без `purge` база и ключи остаются — переустановка их подхватит.

### 6.3 Сервер, установка из исходников

```sh
systemctl disable --now obfmesh-server
obfmesh-ctl teardown --interfaces
rm -rf /opt/obfmesh /etc/obfmesh /var/lib/obfmesh /var/log/obfmesh /run/obfmesh
rm -f /etc/systemd/system/obfmesh-server.service /etc/logrotate.d/obfmesh /usr/local/sbin/obfmesh-ctl
rm -rf /etc/systemd/system/obfmesh-server.service.d
systemctl daemon-reload
```

### 6.4 Общее для обоих способов

Правила NAT и FORWARD общие для всех лучей и ни `purge`, ни `teardown` их не снимают — иначе
удаление одного экземпляра сломало бы соседний. Снимаются вручную:

```sh
iptables -t nat -D POSTROUTING -s 10.77.0.0/16 ! -d 10.77.0.0/16 -j MASQUERADE
iptables -D FORWARD -s 10.77.0.0/16 -j ACCEPT
iptables -D FORWARD -d 10.77.0.0/16 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
for p in $(seq 48201 48210); do iptables -D INPUT -p udp --dport $p -j ACCEPT 2>/dev/null; done
```

Блок обратного прокси из конфигурации Caddy/nginx убирается отдельно, вместе с перезагрузкой
прокси.

---

## Приложение А. Порты, адреса, таблицы

| Что | Где | Значение |
|---|---|---|
| Обфускатор луча i, вход | сервер, наружу | `udp/(port_base + i)`, по умолчанию `48200 + i` |
| WireGuard луча i | сервер, 127.0.0.1 | `udp/(51820 + i)` |
| API | сервер, 127.0.0.1 | `tcp/8080` |
| Обфускатор луча i, вход | роутер, 127.0.0.1 | `udp/(13300 + i)` |
| WireGuard луча i | роутер, 127.0.0.1 | `udp/(51820 + i)` |
| Адрес сервера в луче i | — | `10.77.{i}.1/24` |
| Адрес клиента в луче i | — | `10.77.{i}.{4k+2}/30`, k — слот клиента |
| AllowedIPs пира клиента на `swg{i}` | сервер | `10.77.{i}.{4k+2}/32`, ровно один адрес |
| Таблица маршрутов луча i | роутер | `route_table_base + i`, по умолчанию `51820 + i` |
| Приоритет правила луча i | роутер | `rule_pref_base + i`, по умолчанию `32000 + i` |
| MTU туннеля | обе стороны | `1400`, на интерфейсе и в маршруте |

`port_base` меняется через `PATCH /api/settings`; диапазон обфускаторов не должен пересекаться с
`51820±10` и с портом API — валидация это проверяет и отказывает.

## Приложение Б. Переменные окружения сервера

Задаются в drop-in юнита (`/etc/systemd/system/obfmesh-server.service.d/10-local.conf`), после
правки — `systemctl daemon-reload && systemctl restart obfmesh-server`.

| Переменная | По умолчанию | Что делает |
|---|---|---|
| `OBFMESH_EXTERNAL_HOST` | адрес из маршрута по умолчанию | внешний адрес сервера; читается только при создании строки настроек |
| `OBFMESH_OBFUSCATOR_BIN` | `/usr/local/bin/wg-obfuscator` | путь к бинарнику; там же, читается один раз |
| `OBFMESH_MANAGE_INPUT` | `0`, в юните `1` | вести ли правила INPUT для портов обфускаторов |
| `OBFMESH_STOP_PROCESSES` | `0` | гасить ли обфускаторы при остановке сервиса |
| `OBFMESH_RECONCILE_INTERVAL` | `300` | период фонового reconcile, секунды; `0` — выключить |
| `OBFMESH_API_PORT` | `8080` | порт API; менять вместе с `ExecStart` и блоком прокси |
| `OBFMESH_DB` | `/var/lib/obfmesh/obfmesh.db` | файл базы |
| `OBFMESH_LOG_LEVEL` | `INFO` | уровень лога сервиса |
| `OBFMESH_OBF_VERBOSE` | `1` | подробность лога обфускаторов, 0..4 |
| `OBFMESH_ADMIN_KEY_FILE` | `/etc/obfmesh/admin.key` | файл админ-ключа |
| `OBFMESH_ADMIN_KEY` | — | админ-ключ значением; перекрывает файл |
| `OBFMESH_DOCS` | `0` | `1` открывает `/docs` и `/openapi.json` |

## Приложение В. Опции uci на роутере

Секция `config obfmesh 'main'`, файл `/etc/config/obfmesh` (права 600 — в нём токен; объявлен в
`conffiles`, обновление пакета его не перезаписывает).

| Опция | По умолчанию | Что делает |
|---|---|---|
| `enabled` | `1` | `0` гасит всё и оставляет сервис в простое |
| `server_url` | — | база API, например `https://host/obfmesh` |
| `token` | — | клиентский токен (Bearer) |
| `obfuscator_bin` | `/usr/bin/wg-obfuscator` | путь к бинарнику |
| `poll_interval` | `30` | период опроса и housekeeping, секунды, минимум 5 |
| `watch` | `sse` | `sse` или `poll` |
| `log_level` | `info` | `debug`, `info`, `warn`, `error` |
| `tune` | `1` | применять настройку роутера (`tune.sh`): ядра обфускаторов, RPS, `netdev_budget`, governor |
| `route_table_base` | `51820` | база номеров таблиц: луч i получает `route_table_base + i` |
| `rule_pref_base` | `32000` | база приоритетов правил: луч i получает `rule_pref_base + i` |
| `tls_insecure` | `0` | `1` отключает проверку сертификата (только для диагностики) |
| `ca_file` | — | PEM с корневым сертификатом сервера |
| `api_key` | — | админ-ключ; нужен для `obfmesh spokes N` |
| `sse_max_time` | `900` | предельное время одного SSE-соединения, секунды |
| `http_timeout` | `20` | таймаут обычных REST-вызовов, секунды |
| `keepalive` | `25` | `persistent-keepalive` на лучах, секунды |
| `obfuscator_mask_value` | — | перекрыть тип маскировки из бандла; трогать только для проверки гипотез |
| `obfuscator_extra_args` | — | дополнительные аргументы обфускатору |

Опций `agg_iface`, `agg_address`, `route_table`, `rule_pref` и `fwmark` в 1.2.0 нет: единого
агрегирующего устройства и единой таблицы туннеля больше не существует. Если раньше трафик
заворачивался в туннель по метке, теперь метка направляется в конкретный луч своим правилом,
поставленным рядом с obfmesh:

```sh
ip rule add fwmark 0x1 lookup 51821 pref 31001     # метка 0x1 → первый луч
```

Такое правило obfmesh не ведёт и при остановке не снимает — это ваша настройка.
