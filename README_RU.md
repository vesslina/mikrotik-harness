# MikroTik Harness

<p align="center">
  <img src="pic-git.PNG" alt="MikroTik Harness" width="760">
</p>

<p align="center">
  CLI-окружение для LLM-агентов, работающих с RouterOS.
</p>

<p align="center">
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/version-0.1.0-e05d44.svg" alt="версия 0.1.0"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-007ec6.svg" alt="лицензия MIT"></a>
  <img src="https://img.shields.io/badge/Windows-10%2F11-0078D6.svg" alt="Windows 10 или 11">
  <img src="https://img.shields.io/badge/python-3.11%20%7C%203.12-3776AB.svg" alt="Python 3.11 или 3.12">
  <img src="https://img.shields.io/badge/node-22%2B-339933.svg" alt="Node.js 22 или новее">
  <img src="https://img.shields.io/badge/RouterOS-7.x-293845.svg" alt="RouterOS 7.x">
  <img src="https://img.shields.io/badge/backend-MikroMCP%20v1.10.0-6f42c1.svg" alt="MikroMCP v1.10.0">
</p>

[English README](README.md)

MikroTik Harness (`mth`) — любительский CLI-проект, который создаёт полноценное рабочее
окружение для LLM-агента внутри MikroTik RouterOS. Harness находит роутер, подключает выбранную
модель, выдаёт ей разрешённые инструменты, показывает ход работы и оставляет изменения под
контролем оператора.

Типизированный RouterOS backend предоставляет
[MikroMCP](https://github.com/AliKarami/MikroMCP) ([официальный сайт](https://mikromcp.com/)).
`mth` запускает закреплённую версию backend локально и добавляет Discovery, модели, режимы доступа,
подтверждения, историю сессий, офлайн-поиск по справке RouterOS и постоянный SSH-канал для
HIGH RISK.

> [!WARNING]
> Это не официальный продукт MikroTik. Сначала проверяйте изменения на тестовом устройстве.
> HIGH RISK может выполнить любую операцию, разрешённую подключённому пользователю RouterOS.

## Возможности

- Поиск MikroTik по MNDP и подключение по адресу, введённому вручную.
- LM Studio, Ollama и любые OpenAI-compatible chat-completions endpoints.
- Разные полномочия агента в режимах PLAN, READY и HIGH RISK.
- Живой каталог MikroMCP вместо жёстко зашитого числа инструментов.
- Вызовы tools, reasoning, подтверждения, проверка результата и ответ агента в одном CLI.
- Локальные presets моделей и история диалогов; ключи защищены хранилищем пользователя Windows.
- Переносимый офлайн-поиск по официальному manual RouterOS без отдельной embedding-модели.
- Pre-flight backup и обязательный Safe Mode перед разблокировкой HIGH RISK.

## Режимы агента

Режимы переключаются клавишей `Tab`.

| Режим | Доступ агента | Для чего нужен |
| --- | --- | --- |
| **PLAN** | Живые read-only инструменты MikroMCP | Инвентаризация, диагностика и планирование без изменений |
| **READY** | Read-tools и проверенные proposal/runbook workflows | Обычные изменения с preview, подтверждением, применением и проверкой |
| **HIGH RISK** | Возможности READY, живой каталог MikroMCP и постоянный RouterOS CLI по SSH | Свободная инженерная работа, когда готового workflow недостаточно |

READY не отдаёт модели каждый raw write-tool напрямую. Поддерживаемое изменение сверяется с
живым состоянием, показывается оператору, подтверждается, применяется через MikroMCP и проверяется.

HIGH RISK сознательно снимает это ограничение. Перед входом `mth` закрепляет SSH host key,
создаёт и скачивает binary backup и text export, проверяет оба файла, открывает один постоянный
AsyncSSH PTY и подтверждает RouterOS Safe Mode. Все вызовы `ssh_exec` идут через тот же канал,
поэтому CLI-контекст и Safe Mode не теряются между командами. При выходе нужно явно применить
изменения или откатить их через Safe Mode. `/rollback` отдельно подтверждает загрузку полного
backup и перезагружает роутер.

## Системные требования

Для текущей установки из исходников нужны:

- 64-битная Windows 10 или Windows 11;
- CPython 3.11 или 3.12;
- Node.js 22 или новее и npm;
- Git для репозитория и submodule MikroMCP;
- RouterOS 7.x с HTTPS REST (`www-ssl`); для HIGH RISK также нужен SSH;
- LM Studio, Ollama или другой OpenAI-compatible endpoint.

PowerShell 7, Visual Studio, C++ Build Tools и отдельный Microsoft VC++ Redistributable не нужны,
если pip устанавливает готовые binary wheels. Для команд установки достаточно встроенного в
Windows 10 Windows PowerShell 5.1.

## Установка из исходников на Windows

Клонируйте репозиторий рекурсивно: обычный `git clone` **не** скачивает содержимое MikroMCP
submodule.

```powershell
git clone --recurse-submodules https://github.com/vesslina/mikrotik-harness.git
cd mikrotik-harness

py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
npm --prefix external/mikromcp ci
npm --prefix external/mikromcp run build
.\.venv\Scripts\python.exe -m pip install -e .
```

Прямой запуск:

```powershell
.\.venv\Scripts\mth.exe
```

После активации окружения команда доступна как `mth`:

```powershell
.\.venv\Scripts\Activate.ps1
mth
```

Если репозиторий уже клонирован без submodule:

```powershell
git submodule update --init --recursive
```

## Однократная подготовка RouterOS

MikroMCP использует HTTPS REST через `www-ssl`; `api-ssl` — другой сервис. Перед вставкой замените
оба `<ROUTER_IP>` на постоянный management IP вашего MikroTik:

```routeros
/certificate add name=mth-ca common-name=mth-ca key-usage=key-cert-sign,crl-sign
/certificate sign mth-ca
/certificate add name=mth-https common-name=<ROUTER_IP> subject-alt-name=IP:<ROUTER_IP> key-usage=tls-server
/certificate sign mth-https ca=mth-ca
/ip service set www-ssl port=443 certificate=mth-https disabled=no
/ip service set ssh disabled=no
```

Используйте RouterOS-аккаунт с непустым паролем и только необходимыми политиками. При первом
подключении harness показывает TLS fingerprint. HIGH RISK отдельно показывает и закрепляет SSH
host-key fingerprint. Последующее несовпадение fingerprint — причина остановиться, а не пропустить
предупреждение.

## Первая сессия

1. Запустите `mth` и выберите роутер в Discovery либо введите management IP вручную.
2. Введите логин и пароль RouterOS и подтвердите проверенный TLS fingerprint.
3. Выполните `/model`, выберите **Локальная модель** или **OpenAI-compatible provider** и сохраните
   preset.
4. Начните с разведки в PLAN. Переключайте `Tab` только тогда, когда задаче действительно нужны
   дополнительные полномочия.

Для Ollama запустите `ollama serve`, загрузите модель с поддержкой tool calls и используйте
`http://127.0.0.1:11434/v1`. Для LM Studio включите локальный OpenAI-compatible server и укажите
показанный им URL. Сами LM Studio и Ollama в состав `mth` не входят.

## Офлайн manual RouterOS

Один раз соберите пакет на компьютере с интернетом:

```powershell
mth rag
mth rag --query "safe mode rollback"
```

По умолчанию пакет находится в `.mth/rag`. Скопируйте всю эту папку на офлайн-ноутбук в то же
место или задайте её через `MTH_RAG_HOME`. Готовый pack проверяется по checksum и открывается без
сетевых запросов. Поиск использует встроенный SQLite FTS5, поэтому Chroma и отдельная
embedding-модель не нужны. URL рядом с результатами — сохранённая локально ссылка на источник;
агент её не открывает.

Проектные полевые рецепты лежат в [`docs/field-recipes`](docs/field-recipes). Новая Markdown-карточка
в этой папке становится доступна через `search_field_recipes` без скачивания.

Публичный репозиторий не распространяет корпус документации MikroTik. Соберите его из официального
источника либо перенесите собственную проверенную копию с соблюдением условий документации.

## Команды

```text
/help       справка
/info       устройство и модель
/model      добавить или изменить preset
/models     выбрать или удалить preset
/language   русский или английский
/new        новый чат
/history    список сессий
/resume     последняя сессия
/log        локальный audit transcript
/clear      очистить transcript и память модели
/rollback   preview и подтверждение rollback
/exit       выйти из чата
```

Discovery без UI:

```powershell
mth discover
mth discover --json
mth discover --broadcast 192.168.56.255
```

## Локальные данные

Приватное состояние хранится в `.mth/` и исключено из Git: регистрация роутеров, trust-записи,
зашифрованные secrets провайдеров, история runbook-ов и чатов, recovery-файлы HIGH RISK и
опциональный manual pack. Не публикуйте эту папку.

## Офлайн-установка на полевой ноутбук

Копирование готовой `.venv` с другого компьютера не поддерживается: Windows venv содержит
машинно-зависимые пути. Цель релиза 1.0 — per-user offline bundle с приватными CPython и Node.js,
собранным MikroMCP, wheelhouse Python и опциональным пользовательским RAG pack. На целевом ноутбуке
не потребуются Git, npm, глобальные Python/Node, права администратора и интернет.

Зафиксированные структура bundle, алгоритм установки, список зависимостей и clean-machine matrix
описаны в [Windows offline distribution](docs/windows-offline-distribution.md). Пока пакет не прошёл
чистые тесты на Python 3.11 и 3.12, единственный поддерживаемый способ — установка из исходников
выше.

## Проверки для разработчика

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\mypy.exe src
.\.venv\Scripts\python.exe -m mth --help
```

Архитектура и безопасность описаны в
[`docs/block-b-architecture.md`](docs/block-b-architecture.md),
[`docs/high-risk-mode.md`](docs/high-risk-mode.md) и
[`docs/rag-packs.md`](docs/rag-packs.md).

## Лицензия

MikroTik Harness распространяется по [лицензии MIT](LICENSE). MikroMCP и другие сторонние
компоненты сохраняют собственные лицензии.
