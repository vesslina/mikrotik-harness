# MikroTik Harness

[English README](README.md)

`mth` — ориентированный на безопасность harness для управления RouterOS через закреплённый
backend MikroMCP. Проект включает Block A и рабочий Block B: поиск устройств по MNDP,
trust-on-first-use для TLS, регистрацию backend, presets моделей, маршрутизацию read-only
инструментов по capability packs и семь runbook'ов с dry-run, подтверждением, проверкой,
журналом и rollback.

## Установка для разработки

```powershell
python -m venv .venv
git submodule update --init
npm --prefix external/mikromcp ci
npm --prefix external/mikromcp run build
.venv\Scripts\python -m pip install -e ".[dev]"
```

MikroMCP подключён как git submodule и закреплён на `v1.9.0`. Harness не изменяет его исходники.
Если npm недоступен, тот же backend можно собрать локальным `pnpm`.

## Терминальный интерфейс

Запуск:

```powershell
mth
```

Discovery автоматически ищет соседей MNDP. Стрелки выбирают устройство, `Tab` перемещает фокус,
`r` обновляет список, `q` завершает программу. Пароль RouterOS всегда вводится в masked field.

- Выбор строки переносит её IP в поле подключения.
- Первое соединение показывает SHA-256 fingerprint TLS. Его необходимо сверить с доверенным
  источником; последующее несовпадение останавливает соединение до отправки credentials.
- Данные сохраняются в приватной `.mth/`, которая исключена из Git.
- Backend запускается от ограниченной identity `mth-operator`, получает живой каталог tools и
  проверяет роутер через `check_router_health`.
- После успешной проверки открывается чат с профилем RouterOS и количеством доступных MCP tools.

Discovery и чат используют общую чёрно-красно-белую тему. Выбор темы Textual оставлен доступным,
но базовая цветовая идентичность harness сохраняется.

### Подготовка HTTPS REST на RouterOS

RouterOS REST работает через `www-ssl`; `api-ssl` — другой бинарный API. На тестовом RouterOS 7
можно создать локальный CA и серверный сертификат так:

```routeros
/certificate add name=mth-ca common-name=mth-ca key-usage=key-cert-sign,crl-sign
/certificate sign mth-ca
/certificate add name=mth-https common-name=192.168.56.103 subject-alt-name=IP:192.168.56.103 key-usage=tls-server
/certificate sign mth-https ca=mth-ca
/ip service set www-ssl port=443 certificate=mth-https disabled=no
```

IP должен быть стабильным management-адресом роутера. Если `api-ssl` ранее переносили на 443,
верните его на штатный порт:

```routeros
/ip service set api-ssl port=8729 disabled=yes
```

Используйте отдельного пользователя RouterOS с минимальными необходимыми правами и непустым
паролем. MNDP — недоверенное сетевое объявление; непрерывность устройства устанавливается только
закреплённым TLS fingerprint.

## Чат и агент

Шапка всегда показывает подключённый MikroTik, версию RouterOS, выбранную модель, провайдера,
версию harness и живое количество MCP tools. Пиксельный логотип имеет смещённую терминальную
тень в стиле классического wordmark Claude Code, сохраняя собственные красный, белый и чёрный
цвета проекта.

Основные команды:

- `/model` — добавить или изменить модель.
- `/models` — выбрать или удалить сохранённый preset вместе с его API-ключом.
- `/language` — выбрать русский или английский; также работают `/language ru` и `/language en`.
- `/pppoe`, `/bridge`, `/dhcp`, `/dns`, `/nat`, `/services`, `/wireguard` — открыть безопасный
  schema-driven runbook в READY.
- `/rollback [execution-id|journal-id]` — показать preview и откатить полное выполнение runbook.
- `/help`, `/info`, `/log`, `/clear`, `/exit` — остальные команды.

При первом запуске язык определяется по locale операционной системы. Выбранное значение хранится
в `.mth/settings.json`. `/clear` очищает и видимый transcript, и in-process память модели.

Все взаимодействия выполняются inline. Model form, выбор preset, runbook form, apply/rollback,
удаление модели и язык временно заменяют composer внизу, не закрывая transcript. `Esc` отменяет
действие, `Tab` в approval возвращает к изменению параметров. Опции «разрешить все изменения на
сессию» нет намеренно: каждый неизменяемый план требует отдельного human approval.

Сообщения пользователя отображаются на серой подложке, ответы модели остаются на чёрном фоне.
Во время запроса видны меняющаяся activity-фраза и живое время выполнения. Точное число reasoning
tokens показывается после ответа, если провайдер его вернул. Для non-streaming API live-счётчик
токенов недостоверен, поэтому до ответа интерфейс честно показывает `…`. Между раундами tool calls
появляются в transcript сразу; `Ctrl+O` раскрывает имена и привязанные аргументы инструментов.

`Tab` в обычном composer переключает режимы:

- `PLAN` — полностью без инструментов и без запуска MikroMCP.
- `READY` — модель сначала выбирает capability pack, затем получает только соответствующие
  read-only tools и harness-owned `propose_*` handoffs.

Модель никогда не получает backend write tools, `apply_plan` или `run_command`. Изменение идёт по
цепочке: proposal → редактируемая форма → live dry-run → human approval → backend confirmation →
apply → post-check → journal. Rollback требует отдельного preview и подтверждения.

## Модели и API-ключи

Preset содержит URL, имя модели и capability metadata. API-ключ хранится отдельно в
зашифрованном `.mth/provider-secrets.json`:

- Windows DPAPI для текущего пользователя используется в первую очередь;
- Fernet с приватным `.mth/provider-secrets.key` служит fallback;
- Base64 кодирует уже зашифрованные bytes и не считается шифрованием.

Именованная env-переменная, если она задана и непуста, имеет приоритет над vault. Ключи не
попадают в `providers.json`, transcript, планы, history, логи и Git. Удаление preset удаляет и его
vault entry.

Память диалога ограничена размером контекста, указанным в preset, и сохраняет только последние
полные пары user/assistant. Скрытые reasoning traces туда не копируются.

## Границы безопасности

Результаты MCP рекурсивно редактируются перед передачей внешней модели. Поля password, token,
private key, community и похожие скрываются. Только loopback-модель может получить явное
разрешение видеть sensitive read data.

Runbook baselines имеют независимую allowlist-проекцию: произвольные комментарии, scripts и
секреты RouterOS не становятся долговременным состоянием harness. Router ID каждого tool call
всегда заменяется ID текущего подключённого устройства.

Точные ограничения закреплённого MikroMCP описаны в
[`docs/backend-capability-gaps.md`](docs/backend-capability-gaps.md). Сейчас нельзя честно считать
готовыми полный DHCP network/gateway, Wi-Fi password, baseline firewall, backup/export workflow и
изменение SSH port.

## Headless discovery

```powershell
mth discover
mth discover --json
```

При нескольких сетевых адаптерах можно явно передать directed broadcast:

```powershell
mth discover --broadcast 192.168.56.255
```

MAC-only подключения RouterOS не входят в v1.

## Проверки

```powershell
pytest
ruff check .
mypy src
python -m mth --help
```

Архитектура Block B находится в [`docs/block-b-architecture.md`](docs/block-b-architecture.md),
трёхуровневые сценарии проверки моделей — в
[`docs/model-evaluation-prompts-ru.md`](docs/model-evaluation-prompts-ru.md).
