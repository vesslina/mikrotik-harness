---
kind: field_recipe
collection: rag2b_field
id: sxtsq-lite5-subscriber-cpe
status: reviewed-template
device_models:
  - SXTsq Lite5
  - RBSXTsq5nD
routeros_cli_surface: legacy-wireless
start_state: bare-routeros-after-approved-no-defaults-reset
not_safe_to_rerun_without_state_checks: true
required_interfaces:
  - ether1
  - wlan1
required_packages:
  - wireless
deployment_role: subscriber-cpe
---

# SXTsq Lite5 — стандартная абонентская CPE

Это полевой шаблон для одинаково настроенных абонентских точек MikroTik
SXTsq Lite5 (модель RBSXTsq5nD). Устройство работает как наружная 5 GHz CPE:
оно подключается к секторному MikroTik по радио, передаёт провайдерские VLAN
605/808 через два bridge и поднимает PPPoE поверх `br.808`. Клиентская сеть
выдаётся через `ether1`.

Карточка описывает конкретную корпоративную схему, а не универсальную настройку
RouterOS. Применять её можно только после проверки устройства и входных данных.

## Проверенные аппаратные границы

- 5 GHz 802.11a/n, встроенная направленная антенна 16 dBi.
- Один Fast Ethernet 10/100; это не гигабитная модель.
- 64 MB RAM, 16 MB Flash, CPU AR9344 600 MHz, license level 3.
- Passive PoE, 10–30 V DC; максимальное потребление около 6 W.
- Шаблон использует старый интерфейс `/interface wireless` и `station-bridge`.
  Если на устройстве есть только `/interface wifi`, этот рецепт нельзя выполнять
  без отдельной адаптации.

Официальное описание SXTsq указывает, что `station-bridge` работает с
RouterOS AP и не является универсальным режимом для стороннего AP. Если AP не
MikroTik или не разрешает station-bridge, остановись и запроси другой дизайн
(обычно routed station или station-pseudobridge).

## Стоп-условия до первой команды

Остановись и задай вопрос оператору, если выполнено хотя бы одно условие:

1. Модель не `RBSXTsq5nD`, нет `wlan1`, нет legacy-пакета `wireless`, или устройство
   является CHR/обычным роутером.
2. Не известны все значения из раздела «Поля установки».
3. Не подтверждён полный сброс конфигурации. Первая команда удаляет текущую
   конфигурацию и перезагружает устройство; запускать её можно только после
   отдельного backup и явного подтверждения.
4. Нет физического/MAC-пути для повторного входа после сброса. Не рассчитывай,
   что старая IP-сессия переживёт reset.
5. `country=debug`, `frequency-mode=superchannel`, `allow-none-crypto=yes` или
   `forwarding-enabled=remote` не разрешены оператором и местными правилами.
   Это особенности исходной полевой конфигурации, а не безопасные значения по
   умолчанию.

Перед применением сначала прочитай состояние: модель/board, RouterOS и пакеты,
список интерфейсов, wireless-интерфейс, bridge/VLAN, IP-адреса, PPPoE-клиенты,
маршруты и сервисы. Не угадывай недостающие значения.

## Поля установки

Подставляй значения только в эти placeholders. Не помещай реальные секреты в
RAG, историю, план или сообщение модели.

| Placeholder | Смысл | Пример формата |
| --- | --- | --- |
| `{{DEVICE_IDENTITY}}` | имя точки в `/system identity` | `city-street-14` |
| `{{RADIO_NAME}}` | уникальный `radio-name` | `city-street-14_15.61` |
| `{{ADMIN_PASSWORD}}` | новый пароль администратора, masked input | не сохранять |
| `{{ACTUAL_COUNTRY}}` | разрешённая страна установки | `...` |
| `{{SECTOR_SSID}}` | SSID провайдерского сектора | `adopt_sector1` |
| `{{SECURITY_PROFILE}}` | согласованный wireless security profile | `default` или имя профиля |
| `{{PPPOE_USER}}` | логин провайдера | `subscriber-001` |
| `{{PPPOE_PASSWORD}}` | пароль провайдера, только masked input | не сохранять |
| `{{ANTENNA_GAIN_DB}}` | значение RF-плана, не угадывать | `16` |
| `{{PROVIDER_IP_CIDR}}` | IP провайдера на `br.605` | `172.20.10.14/24` |
| `{{PROVIDER_NETWORK}}` | сеть для этого адреса | `172.20.10.0` |

Фиксированные значения этого профиля: VLAN 605/808, `br.605`, `br.808`,
PPPoE `pppoe-out1`, LAN `192.168.88.0/24` на `ether1`. SSID, страна, security
profile и RF-параметры должны быть подтверждены оператором; пример `adopt_sector1`
не является универсальным значением.

## Особое правило для 192.168.88.1/24

Команда добавления `192.168.88.1/24` нужна только если адрес отсутствует.
Если live-проверка уже показывает `192.168.88.1/24` на `ether1`, не добавляй его
повторно и не создавай duplicate. После `no-defaults` адрес обычно исчезает, поэтому
проверку нужно повторить после повторного подключения к чистому устройству.

## Золотой порядок применения

Ниже приведён очищенный шаблон успешной полевой настройки. Первая строка —
разрушающая операция, остальные строки должны выполняться только после её
подтверждения и повторного входа.

```routeros
/system reset-configuration no-defaults=yes

/system identity set name={{DEVICE_IDENTITY}}
/system clock set time-zone-name=Europe/Moscow
/system package update set channel=long-term
/system routerboard settings set auto-upgrade=yes

/interface bridge add fast-forward=no name=br.605 protocol-mode=none
/interface bridge add fast-forward=no name=br.808 protocol-mode=none

/interface vlan add interface=wlan1 name=wlan1.605 vlan-id=605
/interface vlan add interface=wlan1 name=wlan1.808 vlan-id=808

/interface wireless set [ find default-name=wlan1 ] antenna-gain={{ANTENNA_GAIN_DB}} band=5ghz-a/n country={{ACTUAL_COUNTRY}} disabled=no frequency=auto frequency-mode=regulatory-domain mode=station-bridge name=wlan2 radio-name={{RADIO_NAME}} security-profile={{SECURITY_PROFILE}} ssid={{SECTOR_SSID}} station-roaming=enabled

/interface ethernet set [ find default-name=ether1 ] advertise=10M-half,10M-full,100M-half,100M-full

/interface bridge port add bridge=br.605 interface=wlan1.605
/interface bridge port add bridge=br.808 interface=wlan1.808

/interface pppoe-client add add-default-route=yes disabled=no interface=br.808 name=pppoe-out1 password={{PPPOE_PASSWORD}} use-peer-dns=yes user={{PPPOE_USER}}

/interface list add comment=defconf name=WAN
/interface list add comment=defconf name=LAN
/interface list add name=mgmt
/interface list member add comment=defconf interface=ether1 list=LAN
/interface list member add interface=pppoe-out1 list=WAN
/interface list member add interface=br.605 list=mgmt

# Выполняй только если /ip address print не показывает этот адрес на ether1.
/ip address add address=192.168.88.1/24 comment=defconf interface=ether1 network=192.168.88.0
/ip address add address={{PROVIDER_IP_CIDR}} interface=br.605 network={{PROVIDER_NETWORK}}

/ip pool add name=default-dhcp ranges=192.168.88.10-192.168.88.254
/ip dhcp-server add address-pool=default-dhcp disabled=no interface=ether1 name=defconf
/ip dhcp-server network add address=192.168.88.0/24 comment=defconf gateway=192.168.88.1

/ip dns set allow-remote-requests=yes
/ip dns static add address=192.168.88.1 name=router.lan
/ip firewall nat add action=masquerade chain=srcnat ipsec-policy=out,none out-interface=pppoe-out1 src-address=192.168.88.0/24

# Осознанно сохранено из корпоративного профиля; это не безопасный default.
/ip ssh set allow-none-crypto=yes forwarding-enabled=remote
/interface wireless security-profiles set [ find default=yes ] supplicant-identity=MikroTik
/user group set full policy=local,telnet,ssh,ftp,reboot,read,write,policy,test,winbox,password,web,sniff,sensitive,api,romon,dude,tikapp
/tool mac-server set allowed-interface-list=LAN
/tool mac-server mac-winbox set allowed-interface-list=LAN
/ip neighbor discovery-settings set discover-interface-list=mgmt
```

Не повторяй `interface=wlan1` после переименования: в шаблоне VLAN создаются до
переименования радио, поэтому `wlan1.605` и `wlan1.808` сохраняют ожидаемое имя.
Если конкретная версия RouterOS ведёт себя иначе, остановись и сверяй live state,
а не исправляй порядок наугад.

В исходном полевом списке были `country=debug`, `frequency-mode=superchannel`,
`antenna-gain=0`, открытый/неуказанный security profile и `1000M` advertise.
Для SXTsq Lite5 это нельзя считать безопасным или даже совместимым default:
модель имеет Fast Ethernet 10/100 и встроенную антенну 16 dBi. Если именно эти
legacy-значения обязательны для конкретного сектора, применяй их отдельным
явным исключением после проверки местных радиоправил и RF-плана:

```routeros
/interface wireless set [ find default-name=wlan1 ] antenna-gain=0 country=debug frequency-mode=superchannel
```

Не выполняй этот override автоматически. Аналогично, `protocol-mode=none`,
`/ip dns set allow-remote-requests=yes`, `allow-none-crypto=yes` и расширенная
политика `full` сохранены только как особенности исходной корпоративной схемы.
Без WAN/LAN firewall-ограничений включённый recursive DNS может стать доступным
из PPPoE/WAN. Это отдельная security-проверка, а не доказательство успеха CPE.

После reset обязательно установи непустой `{{ADMIN_PASSWORD}}` через masked
операторский ввод до подключения устройства к рабочей сети. Пароль не входит в
эту карточку и не должен появляться в prompt, transcript или history.

## Обязательная проверка после применения

Применение считается успешным только если все проверки дают согласованный результат:

```routeros
/system identity print
/interface wireless print detail
/interface wireless registration-table print
/interface bridge print detail
/interface bridge port print
/interface vlan print
/ip address print
/ip route print where dst-address=0.0.0.0/0
/interface pppoe-client print detail
/interface pppoe-client monitor pppoe-out1 once
/ip dhcp-server print detail
/ip firewall nat print where out-interface=pppoe-out1
/ip service print
```

Проверь отдельно: radio connected к ожидаемому AP, оба VLAN находятся в своих
bridge, `pppoe-out1` не disabled и running при наличии линии провайдера,
`192.168.88.1/24` находится на `ether1`, provider IP — на `br.605`, default route
пришла через PPPoE, DHCP выдаёт адреса LAN. Если PPPoE не поднялся, не меняй
пароль наугад: проверь VLAN 808, SSID/радио-регистрацию, bridge и данные провайдера.

## Источники и область действия

- [SXTsq series user manual](https://help.mikrotik.com/docs/spaces/UM/pages/14221556/SXTsq-series)
- [SXTsq Lite5 product specification (RBSXTsq5nD)](https://cdn.mikrotik.com/web-assets/product_files/SXTsq_Lite5_170950.pdf)
- [Wireless station modes](https://help.mikrotik.com/docs/spaces/ROS/pages/122388518/Wireless%2BStation%2BModes)
- [RouterOS configuration reset](https://help.mikrotik.com/docs/spaces/ROS/pages/328155/Configuration%2BManagement)

Эта карточка сейчас является tracked-источником для будущей коллекции RAG 2B.
Она ещё не выбирается автоматически по `device_models`: регистрацию карточек,
метаданные версии/пакетов и детерминированную выдачу после capability selection
нужно выполнить в Pass 4.
