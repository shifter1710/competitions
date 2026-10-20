# Backup reliability, admin health и оповещения

Этот runbook описывает BACKUP-02 / HEALTH-01 / ALERT-01. Он не является
подтверждением успешного production restore или доставки Telegram: это отдельные
операционные проверки. Все примеры ниже используют вымышленные имена и пути.
Команды создания копий, загрузки, восстановления и установки units исполняются
оператором в рамках разрешённой операции, а не при простом чтении инструкции.

## Проверенный комплект

`src/backup.py` и `scripts/backup_sqlite.py` создают:

```text
<output-dir>/sets/<unique-id>/
    database.sqlite3.gz
    files.zip
    manifest.json
```

Manifest содержит версию схемы, уникальный ID, `source` (`daily`, `manual` или
`release`), времена, размеры и SHA256 архивов. Состояния: `Started` → `Completed`
→ `Verified`; сбой записывает отдельный `failed-<id>.json` со статусом `Failed`.
`Completed` ещё не считается успешным комплектом. Публикация каталога в `sets/`
происходит переименованием после повторной проверки `Verified` manifest.

Обязательные проверки: полное чтение gzip, SQLite `PRAGMA quick_check`, ZIP CRC,
безопасность имён файлов, размеры и SHA256, наличие в ZIP файлов, на которые
ссылаются записи вложений и документы календаря. Это не проверка бизнес-содержимого
каждого документа и не полноценный запуск восстановленного приложения.

SQLite online backup API получает снимок живой базы с учётом WAL. До и после
подготовки сравниваются `PRAGMA data_version` и инвентаризация файлов с SHA256;
при обнаруженном изменении подготовка повторяется, максимум три попытки.
Это оптимистическая проверка: она снижает риск несогласованности, но не заменяет
атомарный snapshot или блокировку всех записей приложения. Остаётся риск гонки
изменений/возврата файла к прежнему состоянию между проверками. Для критической
предрелизной точки оператор может согласованно остановить запись; штатный backup
сам веб-приложение не останавливает.

`flock` на `<db-parent>/.backup.lock` сериализует backup jobs одного каталога БД.
Он не блокирует записи Sanic и не является общесистемной блокировкой произвольных
копий БД. Для всех jobs одной БД используйте один канонический путь и владельца.

`files_dir=None` сохраняет совместимость с режимом только БД: такой архив
помечается `database_only`, не считается полным комплектом и не отправляется offsite.
Явно заданный пустой каталог вложений даёт валидный пустой ZIP, если в БД нет ссылок.

**Удаления нет:** `--keep` принимается для совместимости и ничего не удаляет.
Старые локальные архивы и S3-объекты сохраняются; рост диска нужно контролировать.
Retention, Lifecycle, Versioning и очистка требуют отдельного решения владельца.

## Offsite и release-backup

`scripts/backup_offsite.py` выбирает только `Verified` комплекты не старше 26 часов
по времени **создания исходной точки**, повторно проверяет их локально и отправляет
в существующий rclone crypt remote под `sets/<id>/`. Старый плоский формат не
преобразуется автоматически. Selector с `--root` обходит `**/sets/*/manifest.json`.

Архивы отправляются с `--immutable`, затем выполняется `rclone cryptcheck`.
Проверка требует точного списка проверенных файлов, положительного количества
совпадений и `0 differences found`; пропущенные хеши запрещены. Это важно:
rclone 1.60 может вернуть код 0 при отсутствии удалённых хешей.
`--size-only=false` задан явно; неполный отчёт считается ошибкой.
Manifest отправляется после архивов, за ним следует финальная проверка комплекта.
Ошибка не выдаёт успешный статус и не удаляет локальные копии. Прерванный upload
можно повторить для того же неизменённого ID; конфликт существующего содержимого
не разрешается перезаписью. Не изменяйте опубликованные комплекты вручную.

Для release создайте один комплект с `--source release`, затем отправьте именно
его. Не запускайте вторую копию только ради отправки:

```bash
python3 scripts/backup_offsite.py \
    --set-dir /srv/example-app/data/backups/releases/sets/EXAMPLE_SET_ID \
    --remote examplecrypt:example-backups \
    --config /etc/example-app/rclone.conf \
    --state-dir /srv/example-app/data/ops
```

Пример пути должен быть заменён на фактический каталог, возвращённый backup.
Ограничение свежести 26 часов действует и для `--set-dir`; отправка старого
исторического комплекта этим штатным маршрутом не считается свежим backup.

`offsite-receipts.json` хранит `created_at` и `uploaded_at`: повторная отправка
старой точки не делает данные новее. Успешная отправка более старого release не
заменяет ID/время новейшей подтверждённой точки; последняя операция отдельно
записывается в `last_attempt_at`/`last_uploaded_id`/`last_uploaded_created_at`.
Зелёный S3 означает подтверждённую отправку
комплекта и свежую проверку доступности, а не непрерывную проверку каждого объекта.
Health probe `rclone lsf` проверяет доступность remote; он не заменяет `cryptcheck`
и не доказывает, что ранее загруженный комплект не был удалён впоследствии.

## systemd, конфигурация и наблюдение

Шаблоны находятся в `deploy/systemd/`:

| Назначение | Service / timer | Расписание |
|---|---|---|
| Создание | `competitions-backup.*` | ежедневно 03:15 МСК |
| Offsite | `competitions-backup-offsite.*` | ежедневно 04:15 МСК |
| Проверки и alerts | `competitions-health.*` | каждые 15 минут |

При установке проверяйте `OnCalendar`, timezone и реальные следующие запуски.
Не сдвигайте существующее расписание незаметно. Daily и offsite устанавливаются
одним согласованным пакетом: старый uploader корня каталога не увидит `sets/`.
До замены сохраните старые скрипты, units и образ приложения. Проверьте пути,
пользователя, доступ к SQLite/files, rclone crypt и к общему `data/ops`.
Не оставляйте второй cron/timer, запускающий прежний pipeline параллельно.

Offsite и health используют защищённый `/etc/competitions/operations.env`.
Создавайте его вне Git с правами `0600`, каталог — с ограниченным доступом.
Не копируйте production-значения в примеры, CI, отчёты или аргументы команд.
Используемые имена параметров (значения здесь намеренно не приводятся):

- `RCLONE_CONFIG`, `BACKUP_CRYPT_REMOTE` — существующие конфигурация и crypt remote.
- `HEALTH_APP_URL` — локальный `/healthcheck`, по умолчанию loopback на 8081.
- `HEALTH_DISK_WARNING_PERCENT` — порог занятого диска, по умолчанию 85.
- `TELEGRAM_ENABLED` — по умолчанию `false`.
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — только в защищённом окружении.
- `TELEGRAM_TIMEOUT_SECONDS`, `TELEGRAM_RETRIES` — по умолчанию 5 секунд и 2 retry;
  реализация ограничивает timeout 10 секундами и retry тремя.

`/admin/health` — read-only страница только admin. Она читает `data/ops/health.json`;
на HTTP GET не запускаются команды, SQLite integrity check или S3-запросы.
Ревизия на странице пока неизвестна: host Git и статический EnvironmentFile
не доказывают версию работающего образа. Ревизия отдельно проверяется при релизе.
Отдельный collector проверяет loopback healthcheck, SQLite `quick_check`, диск
и доступность crypt remote без импорта Sanic. Состояние обновляется атомарно,
с блокировками; каталоги состояния должны оставаться приватными и не публиковаться.
С `--check-systemd` collector также проверяет Result штатных daily/offsite units,
чтобы сбой запуска не скрывался за предыдущей свежей точкой. Обычное создание
backup имеет grace period 30 минут без alert; зависший запуск становится WARNING.

Обычные проверки становятся просроченными после 30 минут; local/S3 — после
26 часов. Возраст source backup также проверяется отдельно, предел — 26 часов,
что оставляет запас для суточного расписания. Неизвестное, будущее или некорректное
время не становится OK. Полный restore старше 90 дней показывает STALE; отсутствие
реального теста — NEVER TESTED. Даты страницы отображаются в МСК.

Коды ошибок отображаются через фиксированный безопасный словарь, без stack trace,
путей, секретов или содержимого данных. Просмотр журнала systemd оператором остаётся
отдельной приватной диагностикой. Состояния — кэшированные свидетельства: потеря
самого VPS локальными jobs не обнаруживается. Внешний availability-check можно
добавить отдельной задачей; в этом пакете он не устанавливается.

## Telegram

Telegram необязателен. При отсутствии бота, token/chat ID или выключенном флаге
состояние UNKNOWN / «Не настроен», настоящая доставка не подтверждена.
Синтетические тесты используют mock и не отправляют сообщения пользователям.
Для настоящего теста владелец создаёт бота, безопасно задаёт credentials и
подтверждает получателя; только затем разрешена одна контролируемая отправка.

Уведомления формируются из состояний local backup, SQLite/ZIP, S3 upload и
доступности, свежести offsite, диска и restore. Отправляются новая проблема,
ограниченные напоминания и восстановление ранее сообщённой проблемы, без каждого
успешного ежедневного backup. Неизвестное состояние само по себе не считается
восстановлением. Дедупликация хранится в приватном `alert-incidents.json` между
запусками. Между попытками — минимум 15 минут; напоминания — раз в сутки,
не более двух на продолжающуюся проблему. Есть ограниченные retry/timeout и
обработка rate limit. Ошибка доставки не помечается доставленной.

## Изолированное восстановление

Сначала выполните полный drill на полностью синтетическом комплекте. Реальные
архивы содержат персональные данные: не выводите их содержимое, HTTP-страницы,
пользователей или логи приложения в публичные отчёты/скриншоты. Все восстановленные
данные храните вне Git с ограниченными правами, никогда не поверх живого `data/`.

1. Убедитесь, что crypt-конфигурация, пароли и доступ к S3 доступны вне VPS.
   Защищённое локальное устройство владельца может служить независимым местом,
   но наличие, защищённость и возможность чтения надо подтвердить. Проверка и
   escrow выполняются владельцем/Deploy приватно; содержимое не попадает в Git.
2. Выберите конкретный offsite `sets/<id>` и независимо сохранённый SHA256 его
   manifest. Хэш, полученный только из той же скачанной копии, не подтверждает
   доверие. Храните контрольное значение отдельно вместе с recovery-записью.
3. `scripts/restore_backup.py --remote-set` получает комплект через существующий
   crypt remote (rclone расшифровывает), проверяет доверенный manifest hash,
   gzip/SQLite, ZIP, SHA256 и ссылки, извлекает в **новый отсутствующий каталог**.
4. Этот CLI проверяет архивы и файлы, **не ставит restore PASS**. Для полноценного
   drill используйте `scripts/restore_drill.py` с известным проверенным образом.

Пример для вымышленного комплекта; родительские каталоги уже подготовлены приватно,
`downloaded-set` и `extracted-data` ещё не существуют:

```bash
python3 scripts/restore_backup.py \
    --remote-set examplecrypt:example-backups/sets/EXAMPLE_SET_ID \
    --config /etc/example-app/rclone.conf \
    --trusted-manifest-sha256 EXAMPLE_INDEPENDENT_MANIFEST_SHA256 \
    --set-dir /srv/example-recovery/downloaded-set \
    --destination /srv/example-recovery/extracted-data

python3 scripts/restore_drill.py \
    --set-dir /srv/example-recovery/downloaded-set \
    --image example-app:verified-revision \
    --state-dir /srv/example-app/data/ops \
    --revision EXAMPLE_VERIFIED_REVISION \
    --production-drill
```

Drill сам повторно извлекает комплект во временный каталог, запускает Docker с
`--network none`, read-only rootfs и только временными данными, без mount живого
каталога или `.env`. Проверяет `/healthcheck`, `/login` и наличие связанных файлов.
Успех здесь означает этот ограниченный HTTP/file smoke, а не полный тест всех
пользовательских сценариев. Случайно названный контейнер и временный каталог
убираются при завершении; скачанный комплект/первое извлечение остаются приватными
и требуют контролируемой уборки оператором после проверки.

`--production-drill` требует marker доверенного remote download и только тогда
записывает реальное состояние `restore`. Без флага результат записывается отдельно
в `restore_synthetic` и не превращает NEVER TESTED реальной копии в PASS.
Сохраните реальную ревизию проверенного образа, не выдуманное значение из примера.
Эти CLI не заменяют production-БД, не удаляют её WAL/SHM и не переключают трафик.
Реальное восстановление сервиса требует отдельного разрешения: сохранения текущего
состояния, остановки записей, проверки совместимости образа/схемы, согласованной
замены БД и файлов и последующего smoke.

## Rollback без удаления резервных копий

Откат приложения и откат операций резервного копирования разделены. **Не делайте
`git reset` host checkout и не возвращайте старые backup-скрипты/units с ротацией.**
Старый `--keep` удаляет копии; разрешение на выпуск этого пакета этого не разрешает.
Существующие `sets/`, плоские архивы, S3-объекты и production-БД при rollback сохраняются.

### Подготовка до переключения приложения

Из точной проверенной CI/QA ревизии подготовьте приватный NO-DELETE bundle вне Git.
Он содержит только перечисленные Python modules/scripts, `src/__init__.py`, units,
Compose override и SHA256 manifest; конфигурации, данные и секреты туда не копируются.
Его код должен пройти синтетический compatibility smoke до использования с live DB.
Имена ниже вымышленные: замените корень приложения и существующий image tag на реальные.

```bash
# Сохранить именно прежний работающий образ, до recreate контейнера.
backup_previous_image_id=$(docker inspect --format '{{.Image}}' competitions)
backup_previous_tag="competitions:before-backup02-$(date -u +%Y%m%dT%H%M%SZ)"
docker image tag "$backup_previous_image_id" "$backup_previous_tag"
test "$(docker image inspect --format '{{.Id}}' "$backup_previous_tag")" = "$backup_previous_image_id"

# Родитель bundle заранее создан оператором с правами 0700, destination новый.
backup_ops_bundle=/var/lib/competitions/ops-revisions/VERIFIED_REVISION
python3 scripts/prepare_backup_rollback.py \
    --destination "$backup_ops_bundle" \
    --revision VERIFIED_REVISION \
    --previous-image "$backup_previous_tag" \
    --app-root /srv/example-app
python3 "$backup_ops_bundle/scripts/prepare_backup_rollback.py" \
    --destination "$backup_ops_bundle" --verify
```

`VERIFIED_REVISION` заменяется точным SHA проверенного кода; рабочее дерево перед
подготовкой должно быть чистым. Bundle не заменяет независимый DB/files backup.
`systemd/` внутри bundle содержит новые no-delete units с путями к этому неизменяемому
коду и прежним live data. Устанавливайте daily/offsite/health как согласованный пакет,
проверьте реальные 03:15/04:15 МСК и отсутствие второго uploader. Secrets остаются в
прежнем защищённом EnvironmentFile/rclone config, права bundle — 0700/0600.

### Откат только app image

Сохраните новый host operations code, установленные no-delete units и timers.
Подготовленный JSON — допустимый Compose override. Он меняет только image и два
read-only bind mount: pinned `backup.py` и `ops_state.py`. Остальные файлы старого
образа и штатный data mount берутся из основного Compose файла.

```bash
python3 "$backup_ops_bundle/scripts/prepare_backup_rollback.py" \
    --destination "$backup_ops_bundle" --verify
docker compose -f docker-compose.yml -f "$backup_ops_bundle/rollback.override.json" \
    up -d --no-build --pull never competitions
test "$(docker inspect --format '{{.Image}}' competitions)" = "$backup_previous_image_id"
```

Два mount обязательны: старый web endpoint ручного backup иначе вызовет свой старый
удаляющий `backup.py`. Совместимый positional API нового модуля принимает старый
вызов с `keep=1`, но ничего старого не удаляет. Не монтируйте весь новый `src/` поверх
старого приложения. Изменение схемы этим пакетом не вводится; совместимость выбранного
старого образа с текущей БД всё равно проверяется до rollback.

После переключения проверьте running image ID, оба mount source/read-only и их SHA256
из `bundle.json`, healthcheck и безопасный web smoke. Не используйте host HEAD как
доказательство версии работающего образа. На синтетических данных проверьте старый
manual caller с этими двумя модулями, `keep=1` и несколькими старыми архивами: архивы
должны остаться побайтово прежними, а новый комплект — пройти полную проверку.
Репозиторный `src/backup_rollback_test.py` выполняет именно этот compatibility сценарий.

Ограничение старого UI: новый nested set может не скачиваться через старый плоский
маршрут. Создание остаётся безопасным; получение проверенного комплекта выполняет
оператор приватно. Не возвращайте удаляющий модуль ради старой кнопки скачивания.

### Ограниченный ручной fallback при сбое jobs

Если проблема в запуске units/checkout, используйте **тот же проверенный pinned
NO-DELETE bundle**, не старые скрипты. Timers не отключаются. DB lock и offsite lock
сериализуют ручной запуск с ними; fallback — однократная recovery операция, не второй
постоянный scheduler. Пример с вымышленными путями:

```bash
python3 "$backup_ops_bundle/scripts/prepare_backup_rollback.py" \
    --destination "$backup_ops_bundle" --verify
python3 "$backup_ops_bundle/scripts/backup_sqlite.py" \
    --db-path /srv/example-app/data/competitions.sqlite3 \
    --files-dir /srv/example-app/data/files \
    --output-dir /srv/example-app/data/backups/sqlite \
    --source daily --state-dir /srv/example-app/data/ops
python3 "$backup_ops_bundle/scripts/backup_offsite.py" \
    --root /srv/example-app/data/backups \
    --remote examplecrypt:example-backups \
    --config /etc/example-app/rclone.conf \
    --state-dir /srv/example-app/data/ops
```

Если исправный комплект уже создан, при повторе отправки используйте `--set-dir`
его каталога вместо нового создания. Коды возврата, Verified manifest, cryptcheck,
свежесть точки и продолжение штатных jobs должны быть подтверждены; один зелёный
web healthcheck недостаточен. Тест CLI fallback проверяет тот же `sets/<id>` offsite
namespace и отсутствие дополнительных комплектов при повторе отправки.

Bundle не лечит ошибку собственного алгоритма. Если и он не проходит проверку,
остановите автоматический ремонт после установленного лимита, сохраните все копии,
зафиксируйте FAIL/alert и передайте владельцу причину. **Не переключайтесь на старую
удаляющую ротацию и не объявляйте backup успешным.**

Никакие Lifecycle/Versioning/IAM/public policies этим пакетом не меняются.
Retention, удаление копий, перенос crypt-секретов и замена production-БД остаются
за границей автоматического выпуска.
