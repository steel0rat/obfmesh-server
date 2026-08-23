# Выкат obfmesh на боевые машины

Цели: сервер `45.136.127.10` (Ubuntu 24.04, Python 3.12, 4 ядра) и роутер `192.168.2.1`
(FriendlyWrt 25.12.5, NanoPi R3S, aarch64, BusyBox ash, apk, procd).

На роутере работает Forkop и раздаёт интернет домой. Ни один шаг ниже не трогает главную таблицу
маршрутизации, firewall роутера и конфигурацию Forkop: obfmesh создаёт свои интерфейсы `owg{i}`,
свои таблицы `51820+i` и свои правила `32000+i`. Пока секции Forkop не привязаны к лучам, выкат для
домашней сети незаметен, а любой шаг откатывается одной командой.

Порядок: сначала сервер целиком, потом роутер. Обратный порядок бессмыслен — клиенту неоткуда
взять бандл.

Обозначения: `[S]` выполняется на сервере под root, `[R]` — на роутере под root.

Версия 1.2.0 меняет схему: `agg0`, VRF, `teql` и агрегатный адрес удалены, лучи независимы. Если
на машинах стоит 1.1.0, шаги 6 и 7 выполняются как обновление — с проверкой, что от старой схемы
ничего не осталось.

---

## Шаг 0. Подготовка, ничего не меняет

`[S]`

```sh
ssh root@45.136.127.10
uname -a; python3 -V; systemctl --version | head -1
command -v wg ip iptables || echo 'ЧЕГО-ТО НЕТ'
ss -lntp | grep -E ':(443|8080)\b' || echo 'порты 443 и 8080 свободны'
```

`[R]`

```sh
ssh root@192.168.2.1
cat /etc/openwrt_release | head -3; uname -r; nproc
apk info -e ip-full wireguard-tools curl jsonfilter
ls -l /usr/bin/wg-obfuscator
ip route show table 51821; ip route show table 51822    # должны быть пустыми
ip rule show | grep -E '^320[0-9][0-9]:' || echo 'приоритеты свободны'
```

**Проверка.** `wg`, `ip`, `iptables` есть на сервере; на роутере известно, каких пакетов не
хватает, таблицы `51821+` и приоритеты `32001+` свободны.

**Откат.** Не требуется.

Если таблицы или приоритеты заняты чужим сервисом, на шаге 7 задаются другие базы:
`uci set obfmesh.main.route_table_base=...`, `uci set obfmesh.main.rule_pref_base=...`.

---

## Шаг 1. Бинарник обфускатора на сервере

`[S]`

```sh
install -m 0755 wg-obfuscator /usr/local/bin/wg-obfuscator
/usr/local/bin/wg-obfuscator --help | grep -i masking
/usr/local/bin/wg-obfuscator --version
```

**Проверка.** В выводе `--help` есть `masking`, среди значений — `STUN`. Без этого obfmesh
откажется поднимать луч: без маскировки провайдер режет поток, а выглядит это как «туннель
поднят, трафика нет».

**Откат.** `rm -f /usr/local/bin/wg-obfuscator`.

---

## Шаг 2. Установка управляющего сервиса

`[S]`

```sh
apt-get install -y wireguard-tools iproute2 iptables python3-venv
cd /root/obfmesh/server
OBFMESH_EXTERNAL_HOST=45.136.127.10 \
OBFMESH_OBFUSCATOR_BIN=/usr/local/bin/wg-obfuscator \
./install.sh
```

**Проверка.** Скрипт должен закончиться строкой про поднятые лучи:

```sh
systemctl is-active obfmesh-server
obfmesh-ctl status
wg show swg1 | head -5
wg show swg1 allowed-ips          # у пира клиента ровно один /32
ss -lunp | grep -E ':(48201|48202)\b'
pgrep -af wg-obfuscator
```

Ожидается: сервис `active`, два луча (значение по умолчанию), у каждого поднят интерфейс и жив
обфускатор, порты `48201` и `48202` слушаются.

**При обновлении, а не первой установке, лучей останется столько, сколько было.** Значение по
умолчанию 2 действует только для новой базы: миграция чужую настройку не переписывает — это
осознанно, менять число лучей за спиной оператора она не должна. Если на 1.1.0 стояло три,
их и будет три, и суммарная скорость будет 210–222 Мбит/с вместо 359–373. Проверить и поправить:

```sh
obfmesh-ctl status | grep -i spokes
curl -s -H "X-API-Key: <админ-ключ>" http://127.0.0.1:8080/api/settings | grep -o '"spokes":[0-9]*'
```

Менять — с роутера, `obfmesh spokes 2` (шаг 8), либо `PATCH /api/settings` здесь же.

Если что-то не так: `journalctl -u obfmesh-server -n 80`, `tail -50 /var/log/obfmesh/obf1.log`.

**Откат.**

```sh
systemctl disable --now obfmesh-server
obfmesh-ctl teardown --interfaces
rm -rf /opt/obfmesh /etc/obfmesh /var/lib/obfmesh /var/log/obfmesh /run/obfmesh
rm -f /etc/systemd/system/obfmesh-server.service /etc/logrotate.d/obfmesh /usr/local/sbin/obfmesh-ctl
systemctl daemon-reload
```

---

## Шаг 3. Публикация API наружу

Сервис слушает только 127.0.0.1:8080. Без обратного прокси роутер до него не достучится.

`[S]`, вариант с Caddy:

```sh
apt-get install -y caddy
cat /etc/obfmesh/caddy-snippet.txt          # готовый блок
```

Добавить блок в site-блок `/etc/caddy/Caddyfile`. Для голого IP минимальный файл:

```
45.136.127.10 {
    handle_path /obfmesh/* {
        reverse_proxy 127.0.0.1:8080 {
            flush_interval -1
        }
    }
}
```

```sh
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

**Проверка.**

```sh
curl -sk -o /dev/null -w '%{http_code}\n' https://45.136.127.10/obfmesh/api/status
# ожидается 401 — прокси работает, аутентификация на месте

KEY=$(cat /etc/obfmesh/admin.key)
curl -sk -H "X-API-Key: $KEY" https://45.136.127.10/obfmesh/api/status | head -c 200
```

Для голого IP Caddy выписывает сертификат своим внутренним CA. Забираем корневой сертификат — он
понадобится роутеру:

```sh
cp /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt /root/obfmesh-ca.crt
```

**Откат.** Убрать блок из Caddyfile, `systemctl reload caddy`. На obfmesh это не влияет.

---

## Шаг 4. Клиент и токен

`[S]`

```sh
KEY=$(cat /etc/obfmesh/admin.key)
curl -s -X POST http://127.0.0.1:8080/api/clients \
     -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
     -d '{"name":"nanopi"}'
```

Токен в ответе показывается **один раз**. Потеряли — `POST /api/clients` заново под другим именем
либо новый токен тем же способом, каким заводится клиент.

**Проверка.**

```sh
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8080/api/clients
wg show swg1 allowed-ips        # 10.77.1.2/32, ровно один адрес
wg show swg2 allowed-ips        # 10.77.2.2/32
```

Второй адрес у пира — след схемы 1.1.0 с агрегатным адресом; на 1.2.0 его быть не должно.

**Откат.** `curl -s -X DELETE -H "X-API-Key: $KEY" http://127.0.0.1:8080/api/clients/nanopi`

---

## Шаг 5. Проверка портов снаружи

Без этого шага дальше идти нет смысла: клиент не достучится до `48201+`.

С любой третьей машины:

```sh
nc -u -z -w3 45.136.127.10 48201; echo $?
```

`[S]` — параллельно смотрим, дошло ли:

```sh
tcpdump -ni any udp port 48201 -c 5
```

**Проверка.** Пакеты видны в tcpdump. Если нет — смотрите правила: сервис по умолчанию сам
вставляет ACCEPT в INPUT (`OBFMESH_MANAGE_INPUT=1`), но внешний firewall провайдера или ufw может
резать раньше.

```sh
iptables -S INPUT | grep 4820
```

**Откат.** Не требуется.

---

## Шаг 6. Пакеты на роутер

Связь домашней сети на этом шаге не затрагивается. Единственная опасность — `ip-full` вытесняет
`ip-tiny`; ставим одной командой, чтобы роутер не остался без `ip`.

`[R]`

```sh
cp /etc/config/network /etc/config/network.pre-obfmesh
cp /etc/config/firewall /etc/config/firewall.pre-obfmesh
cp /etc/config/obfmesh /root/obfmesh.uci.pre-1.2.0 2>/dev/null   # только при обновлении

apk add ip-full
ip -V && ip rule show | head -3        # ip на месте и умеет rule
```

```sh
apk add --allow-untrusted ./obfmesh-1.3.0-r1.apk ./luci-app-obfmesh-1.3.0-r1.apk
install -m 0755 wg-obfuscator /usr/bin/wg-obfuscator
/usr/bin/wg-obfuscator --help | grep -i masking
```

Пакет `obfmesh-balance` здесь не ставится. Он необязательный, ему нужны уже поднятые лучи и
секции Forkop, переведённые на адрес базового луча, поэтому у него свой шаг 10.

При обновлении с 1.1.0: `/etc/config/obfmesh` объявлен в `conffiles`, поэтому токен и `server_url`
остаются, а новый образец конфига ложится рядом как `.apk-new`. Ручной костыль
`/usr/lib/obfmesh/tune.sh` заменяется файлом из пакета — уберите его вызов из `rc.local`/cron,
чтобы настройка не применялась дважды из двух мест.

**Проверка.**

```sh
ls -l /usr/lib/obfmesh/ /usr/bin/obfmesh     # apply.sh, watcher.sh, tune.sh, lib.sh
obfmesh version                               # 1.3.0
grep -c token /etc/config/obfmesh             # токен пережил обновление
ls /etc/config/obfmesh*                       # obfmesh и, возможно, obfmesh.apk-new
ping -c2 8.8.8.8                              # интернет через Forkop жив
```

**Откат.**

```sh
apk del luci-app-obfmesh obfmesh
apk add ip-tiny        # если нужно вернуть стоковый пакет
```

Обратно на 1.1.0 — установкой прежних `.apk` и восстановлением конфига из
`/root/obfmesh.uci.pre-1.2.0`. Схемы несовместимы: сервер 1.2.0 не отдаёт полей `agg_mode` и
`agg_address`, без которых клиент 1.1.0 отвергает бандл целиком, поэтому откат клиента имеет смысл
только вместе с откатом сервера.

---

## Шаг 7. Настройка и первый запуск на роутере

`[R]`

```sh
scp root@45.136.127.10:/root/obfmesh-ca.crt /etc/obfmesh/server-ca.crt
chmod 600 /etc/obfmesh/server-ca.crt

uci set obfmesh.main.server_url='https://45.136.127.10/obfmesh'
uci set obfmesh.main.token='<токен из шага 4>'
uci set obfmesh.main.obfuscator_bin='/usr/bin/wg-obfuscator'
uci set obfmesh.main.ca_file='/etc/obfmesh/server-ca.crt'
uci commit obfmesh
```

Сначала проверяем доступность сервера, не запуская сервис:

```sh
curl --cacert /etc/obfmesh/server-ca.crt -s -o /dev/null -w '%{http_code}\n' \
     https://45.136.127.10/obfmesh/api/status
# 401 — сервер виден и сертификат принят
```

Если сертификат не принимается, временно `uci set obfmesh.main.tls_insecure=1` — но это открывает
канал управления для перехвата, так что это способ диагностики, а не рабочая настройка.

Запуск:

```sh
/etc/init.d/obfmesh enable
/etc/init.d/obfmesh start
sleep 15
obfmesh status
```

**Проверка.** В `obfmesh status`:

- `config_version` не пустой, применён секунды назад;
- по каждому лучу `LINK up`, `OBFUSC ok:<pid>`, `HANDSHAKE` меньше двух минут;
- у каждого луча свой адрес `10.77.{i}.2/30` и своя таблица `51820+i`;
- `watcher: running`, строки `FAILED` нет.

Дальше — что трафик действительно ходит, по каждому лучу отдельно и не трогая домашнюю сеть:

```sh
ping -c3 -I owg1 10.77.1.1
ping -c3 -I owg2 10.77.2.1
curl --interface owg1 -s https://ifconfig.me; echo      # должен ответить 45.136.127.10
curl --interface owg2 -s https://ifconfig.me; echo
wg show owg1 transfer; wg show owg2 transfer
ip route show table 51821                                # default dev owg1 mtu 1400
ip rule show | grep 5182
```

При обновлении с 1.1.0 — что от старой схемы ничего не осталось:

```sh
ip link show agg0 2>/dev/null && echo 'ОСТАТОК: agg0'
ip route show table 51820 | grep . && echo 'ОСТАТОК: таблица 51820'
ip rule show | grep -E '^30000:' && echo 'ОСТАТОК: правило oif agg0'
ip -4 addr show | grep '10\.77\.0\.' && echo 'ОСТАТОК: агрегатный адрес'
```

**Откат.**

```sh
/etc/init.d/obfmesh stop
/etc/init.d/obfmesh disable
```

Домашний интернет всё это время идёт мимо obfmesh, поэтому откат ничего не рвёт.

---

## Шаг 8. Настройка роутера и замер

Настройка приезжает с пакетом (`/usr/lib/obfmesh/tune.sh`) и применяется при старте и после
каждого `apply`. Без неё два луча дают 210 Мбит/с вместо 359–373, поэтому замер без проверки
настройки бессмыслен.

`[R]`

```sh
taskset -p "$(cat /var/run/obfmesh/obf-c1.pid)"    # current affinity mask: 4  (CPU2)
taskset -p "$(cat /var/run/obfmesh/obf-c2.pid)"    # current affinity mask: 8  (CPU3)
cat /sys/class/net/eth0/queues/rx-0/rps_cpus       # f
sysctl net.core.netdev_budget                      # 600
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor   # performance
```

Замер одного луча (ожидается около 22 МБ/с ≈ 180 Мбит/с):

```sh
curl --interface owg1 -o /dev/null -s -w 'owg1 %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin
```

Замер суммы — два потока одновременно, каждый в своём луче (ожидается 359–373 Мбит/с):

```sh
curl --interface owg1 -o /dev/null -s -w 'owg1 %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin &
curl --interface owg2 -o /dev/null -s -w 'owg2 %{speed_download} B/s\n' \
     https://speed.hetzner.de/100MB.bin &
wait
```

Одним потоком суммы не получить: 180 Мбит/с — потолок одной TCP-сессии, распараллелить её нельзя
(SPEC, «Что проверено и отвергнуто»).

Число лучей меняется на сервере, читается и меняется с роутера:

```sh
uci set obfmesh.main.api_key='<админ-ключ сервера>'
uci commit obfmesh
obfmesh spokes            # прочитать
obfmesh spokes 2          # рабочее значение
```

**Проверка.** `obfmesh status` показывает два луча со свежими рукопожатиями, сумма двух
одновременных замеров — 359–373 Мбит/с.

Третий луч на этой машине проверен и **медленнее**: 210–222 Мбит/с при загрузке процессора
92–98 %. Если всё же меряете — меряйте до и после, а не «на глаз».

**Откат.** `obfmesh spokes 2`. Настройку роутера снимает `uci set obfmesh.main.tune=0` плюс
перезапуск сервиса; прежние значения governor и RPS возвращает перезагрузка.

---

## Шаг 9. Разведение секций Forkop по лучам

Первый шаг, который реально влияет на домашнюю сеть. Делайте, когда шаг 7 стабильно отработал
хотя бы сутки. Именно здесь получается агрегация: разные сервисы уходят в разные лучи.

`[R]` — до переключения зафиксируйте, как есть:

```sh
( umask 077; uci show forkop > /root/forkop.before-obfmesh 2>/dev/null )
chmod 600 /root/forkop.before-obfmesh
ls -l /root/forkop.before-obfmesh          # -rw-------
grep -n bind_interface /root/forkop.before-obfmesh || echo 'привязок не было'
```

**Снимок Forkop — файл с секретом.** В `uci show forkop` попадает `subscription_urls` секции,
которая работает по подписке, — URL с токеном прямо в пути. Файл остаётся в `/root` на роутере:
не копируется на рабочую машину, не кладётся в `/www` (каталог отдаётся наружу через проброс
11228), не вставляется в переписку. `umask 077` действует только на создание, поэтому права
на уже существующий файл доводит `chmod`.

Дальше — точечно, по одной секции: тяжёлое на один луч, мелкое и разговорчивое на другой. `sed`
в первой команде подменяет значения секретных опций на `<есть>` — тем же маркером obfmesh
показывает наличие секрета в своих отчётах. Без него на экран уедет URL подписки.

```sh
uci show forkop | sed -E "s/^([^=]*(urls?|token|key|secret|password|uuid))=.*/\1='<есть>'/" |
	grep -E 'bind_interface|=.*section'

uci set forkop.<секция_1>.bind_interface='owg1'
uci set forkop.<секция_2>.bind_interface='owg2'
uci commit forkop
/etc/init.d/forkop restart
```

**Проверка.**

```sh
curl -s https://ifconfig.me; echo      # с клиента домашней сети — 45.136.127.10
wg show owg1 transfer; wg show owg2 transfer   # растут оба
obfmesh status
```

**Откат.** Вернуть секциям прежнее состояние по снимку `/root/forkop.before-obfmesh` и перезапустить
Forkop. `uci revert` здесь не поможет — изменения уже зафиксированы `uci commit`:

```sh
# привязки не было вовсе — снять опцию
uci delete forkop.<секция_1>.bind_interface
# привязка была другой — вернуть значение из снимка
uci set forkop.<секция_2>.bind_interface='<значение из /root/forkop.before-obfmesh>'
uci commit forkop
/etc/init.d/forkop restart
curl -s https://ifconfig.me; echo      # внешний адрес снова прежний
```

Сеть роутера этот шаг не трогает: правится только конфигурация потребителя. Снимки
`/etc/config/network.pre-obfmesh` и `firewall.pre-obfmesh` из шага 6 лежат на случай, если что-то
правилось руками мимо obfmesh.

---

## Шаг 10. Балансировка соединений по лучам (пакет obfmesh-balance)

Шаг необязательный. В шаге 9 по лучам разведены сервисы, здесь — отдельные соединения: все
балансируемые секции Forkop выходят с адреса одного луча, а каким лучом пойдёт конкретное
соединение, решает цепочка nft. Делается после того, как шаг 9 отработал хотя бы сутки.
Обоснование, устройство цепочки и полный разбор — INSTALL.md, раздел 3.8; здесь порядок и точки
отката.

Нужен obfmesh **1.3.0 или новее**: до неё первый же `obfmesh apply` сносил правила `31000+i`
балансировщика как чужие в своих таблицах.

`[R]` — проверка перед установкой, ничего не меняет:

```sh
obfmesh version | head -1                      # 1.3.0 или новее, иначе дальше нельзя
uname -r                                       # 6.1.141 — ядро FriendlyElec
ls /lib/modules/$(uname -r)/nft_numgen.ko      # модуль в прошивке; kmod-* на неё не ставят
jsonfilter -i /etc/sing-box/config.json -e '@.route.default_mark'   # 134217728 = 0x08000000
```

Установка. Пакет приходит выключенным и в этом состоянии не создаёт ни строки в nft, ни одного
`ip rule`:

```sh
apk add --allow-untrusted ./obfmesh-balance-1.0.0-r1.apk
obfmesh-balance version
obfmesh-balance check          # пакет ещё выключен; смотрим, что помешает включить
```

Секции Forkop переезжают с `bind_interface` на адрес базового луча. `bind_interface` выбирает луч
до `connect()`, а цепочка живёт в `hook output`, то есть уже после, — такая секция в балансировку
просто не попадёт; `routing_mark` не работает вовсе (SPEC.md, «Что проверено и отвергнуто», п. 5).
`commit` и `restart` идут последними и ровно один раз: sing-box перечитывает конфигурацию только
при старте, а рестарт рвёт все соединения через прокси, поэтому — в тихое время.

Состав секций берётся с самого роутера: он живёт в Forkop и меняется без нас, поэтому списка секций
здесь нет. Включённая секция не значит переводимая. На боевом R3S на 22.08.2026 включённых семь, а
`outbound_json` есть у шести: седьмая (`proxysvc`) работает по подписке, узлы ей приходят извне, ни
к какому лучу она не привязана — переводить в ней нечего. Из шести одна (`speedtest`) уже вышла на
адрес базового луча ещё при проверке `routing_mark` — такие пропускаются, переписывать их второй
раз нечего. Цикл, который печатает готовые `uci set` под свой состав и сам пропускает и уже
переведённые секции, и секции без `outbound_json`, — INSTALL.md, 3.8.2.

Снимок `/root/forkop.before-balance` — такой же файл с секретом, как в шаге 9: в нём URL подписки
с токеном. Права `600`, лежит только на роутере, в `/www` и в переписку не уходит.

```sh
( umask 077; uci show forkop > /root/forkop.before-balance )
chmod 600 /root/forkop.before-balance
ls -l /root/forkop.before-balance          # -rw-------, в файле URL подписки с токеном

# состав: все включённые секции — подлежит ли переводу, имя, исходящий, привязка отдельной опцией
for s in $(uci -q show forkop | sed -n 's/^forkop\.\([A-Za-z0-9_]\{1,\}\)=section$/\1/p'); do
	[ "$(uci -q get "forkop.$s.enabled" 2>/dev/null)" = 0 ] && continue
	oj="$(uci -q get "forkop.$s.outbound_json" 2>/dev/null)"
	bi="$(uci -q get "forkop.$s.bind_interface" 2>/dev/null)"
	if [ -n "$oj" ]; then mark='перевод'
	elif [ -n "$bi" ]; then mark='руками'
	else mark='пропуск'
	fi
	printf '%s\t%s\t%s\tbind_interface=%s\n' "$mark" "$s" "$oj" "$bi"
done

# по одной строке на каждую секцию, помеченную «перевод», КРОМЕ тех, где уже стоит
# inet4_bind_address базового луча; тег у секции остаётся прежним
uci set forkop.<секция>.outbound_json='{"type":"direct","tag":"<прежний тег>","inet4_bind_address":"10.77.1.2"}'
uci -q delete forkop.<секция>.bind_interface   # только если привязка стояла отдельной опцией

uci commit forkop
/etc/init.d/forkop restart
```

Включение делается сразу за перезапуском Forkop: между этими двумя блоками все переведённые секции
идут одним базовым лучом, а это около 180 Мбит/с на всех.

```sh
obfmesh-balance check                    # ноль проблем — можно включать
obfmesh-balance on                       # enabled = 1 и сразу применить
/etc/init.d/obfmesh-balance enable
/etc/init.d/obfmesh-balance start        # без демона веса никто не считает
```

**Проверка.**

```sh
obfmesh-balance status                        # WEIGHT и KERNEL совпадают
nft list table inet obfmesh_balance           # карта весов покрывает 0-99 без дыр
ip rule show | grep -E '^310[0-9][0-9]:'      # правило каждого небазового луча
ip rule show | grep -E '^315[0-9][0-9]:'      # и его страховка blackhole
wg show owg1 transfer; wg show owg2 transfer  # растут оба, а не один
obfmesh-balance logs -n 50
```

Ожидается: таблица есть, у каждого небазового луча ровно два правила — `31000+i` в таблицу луча и
`31500+i` с тем же селектором и действием `blackhole`, — колонки `WEIGHT` и `KERNEL` совпадают,
счётчики растут у обоих лучей.

**Откат.** Мягко, дав дожить соединениям, уже переброшенным на небазовые лучи:

```sh
obfmesh-balance off                      # новые соединения идут базовым лучом
                                         # демон НЕ останавливать: он снимет остальное сам
sleep 660                                # OMB_SOFT_GRACE 600 с плюс такт демона
ip rule show | grep -E '^31[05][0-9][0-9]:' || echo 'правил и страховок нет'
nft list table inet obfmesh_balance 2>/dev/null || echo 'таблицы нет'
/etc/init.d/obfmesh-balance stop
/etc/init.d/obfmesh-balance disable
```

Немедленно, ценой зависших соединений, — `obfmesh-balance off` и следом `obfmesh-balance teardown`;
почему именно в таком порядке, написано в «Полном откате». Секции Forkop возвращаются к прежней
привязке по снимку `/root/forkop.before-balance` — в той форме, в какой она там записана
(`outbound_json` целиком или отдельная опция секции), — отдельным шагом и только после того, как
балансировка снята.

---

## Полный откат

`[R]`

```sh
# вернуть секциям потребителя прежний интерфейс (шаг 9)

# балансировщик снимается ПЕРВЫМ, если он ставился: состав лучей он читает у obfmesh,
# и снятый раньше времени obfmesh оставил бы его правила смотреть в пустые таблицы
obfmesh-balance off                      # СНАЧАЛА выключить, иначе демон соберёт всё заново
obfmesh-balance teardown                 # таблица nft, правила 31000+i, страховки 31500+i
apk del obfmesh-balance                  # его prerm делает то же самое ещё раз, это безвредно

/etc/init.d/obfmesh stop
/etc/init.d/obfmesh disable
apk del luci-app-obfmesh obfmesh
rm -rf /etc/obfmesh /var/run/obfmesh
ip link show owg1 2>/dev/null || echo 'лучи убраны'
ip route show table 51821                # должно быть пусто
ip rule show | grep -E '^320[0-9][0-9]:' || echo 'правил нет'
ip rule show | grep -E '^31[05][0-9][0-9]:' || echo 'следов балансировщика нет'
nft list table inet obfmesh_balance 2>/dev/null || echo 'таблицы балансировщика нет'
ping -c2 8.8.8.8
```

`off` перед `teardown` — не вежливость, а условие того, что `teardown` вообще подействует. Он
снимает вместе с таблицей и подписи в `/var/run/obfmesh-balance`, а демон при `enabled = 1` видит
на ближайшем такте, что подписи не сходятся, и собирает таблицу и правила заново — то есть через
`interval` секунд (по умолчанию пять) всё возвращается на место. Дальше `apk del` со своим `prerm`
попадает в ту же ловушку: `prerm` зовёт `teardown` до того, как apk остановит сервис.

`teardown` здесь не лишний шаг. Остановка сервиса — `/etc/init.d/obfmesh-balance stop` — **мягкая**:
она оставляет таблицу и правила в ядре ради соединений, уже переброшенных на небазовые лучи. Всё
своё снимает только `teardown`, сам по себе или из `prerm`. Эти соединения после него не рвутся, а
виснут до таймаута приложения: запись conntrack переживает снятие таблицы, а сбросить её нечем —
утилиты `conntrack` на роутере нет.

`[S]`

```sh
systemctl disable --now obfmesh-server
obfmesh-ctl teardown --interfaces
iptables -t nat -D POSTROUTING -s 10.77.0.0/16 ! -d 10.77.0.0/16 -j MASQUERADE
iptables -D FORWARD -s 10.77.0.0/16 -j ACCEPT
iptables -D FORWARD -d 10.77.0.0/16 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
for p in $(seq 48201 48210); do iptables -D INPUT -p udp --dport $p -j ACCEPT 2>/dev/null; done
rm -rf /opt/obfmesh /etc/obfmesh /var/lib/obfmesh /var/log/obfmesh /run/obfmesh
rm -f /etc/systemd/system/obfmesh-server.service /etc/logrotate.d/obfmesh /usr/local/sbin/obfmesh-ctl
systemctl daemon-reload
```

---

## Что смотреть, когда не работает

| Симптом | Где смотреть |
|---|---|
| `obfmesh status` пуст, бандла нет | `obfmesh logs -n 50`; проверьте `server_url`, `token`, `ca_file` |
| Луч поднят, рукопожатия нет | путь до `48200+i`: `tcpdump -ni any udp port 48201` на сервере |
| Обфускатор `DEAD` на роутере | `cat /var/run/obfmesh/obf-c1.log`; частая причина — занятый порт `13301` |
| Обфускатор падает на сервере | `tail -50 /var/log/obfmesh/obf1.log`, `journalctl -u obfmesh-server -n 80` |
| Рукопожатия идут, трафика нет | маскировка: `grep masking /etc/obfmesh/obf1.conf` и `/var/run/obfmesh/obf-c1.conf` — значение должно совпадать байт в байт (`STUN`) |
| Через `owg{i}` не ходит ничего | `ip route show table 51820+i`, `ip rule show`, `wg show swg{i} allowed-ips` |
| Ответы приходят не в тот луч | в AllowedIPs пира на сервере больше одного адреса — след 1.1.0 |
| Скорость около 210 вместо 359 Мбит/с | настройка роутера не применилась: `taskset`, `rps_cpus`, `netdev_budget` из шага 8 |
| Скорость как у одного луча | все секции Forkop сидят на одном интерфейсе: `wg show owg2 transfer` не растёт |
| Один поток не выше ~180 Мбит/с | это потолок одной сессии, не поломка |
| Сервис на роутере «останавливается» и встаёт обратно | `ls /var/run/obfmesh/admin-down`; watcher уважает этот маркер |
| После обновления пропал токен | конфиг перезаписан мимо пакетного менеджера; `/etc/config/obfmesh` обязан быть в `conffiles` |

Ключи, токены и `obfuscation_key` в логи и в вывод команд не попадают: в отчётах на их месте
`<есть>`. Если секрет всё же виден в выводе — это ошибка, а не особенность.
