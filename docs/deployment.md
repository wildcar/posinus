# Развёртывание posinus на Ubuntu

Инструкция рассчитана на чистый односерверный production-хост и запуск команд из обычной учётной записи с правами `sudo`. Имя этой учётной записи не используется в путях и правах. Краулер работает под отдельным системным пользователем `posinus` без интерактивного входа, конвейер оценки и публикации — под `posinus-pipeline`.

Шаги 1–14 ставят краулер. Конвейер добавляется шагом 15 и требует уже установленного краулера: он читает базу краулера через групповой доступ.

Основной вариант — Ubuntu 24.04 LTS со штатными Python 3.12 и пакетом [python3-venv](https://packages.ubuntu.com/noble/python/python3-venv). Также поддерживаются Python 3.13 и 3.14 на более новых Ubuntu. Не заменяйте системный `/usr/bin/python3` вручную.

## Итоговая структура

| Назначение | Путь | Владелец и доступ |
|---|---|---|
| Checkout репозитория | `/opt/posinus` | `root:root`, сервисы только читают |
| Краулер и его virtualenv | `/opt/posinus/crawler` | `root:root` |
| Конвейер (три скрипта) | `/opt/posinus/pipeline` | `root:root` |
| Конфигурация краулера | `/etc/posinus/crawler.env` | `root:posinus`, `0640` |
| Конфигурация конвейера | `/etc/posinus/pipeline.env` | `root:posinus-pipeline`, `0640` |
| SQLite, backup, Chromium | `/var/lib/posinus` | группа `posinus`, setgid/default ACL |
| Основная база | `/var/lib/posinus/posinus.sqlite3` | `posinus:posinus`, `0660` |
| Состояние конвейера | `/var/lib/posinus/pipeline` | `posinus-pipeline:posinus-pipeline`, `0750` |
| Логи краулера | `/var/log/posinus` | `posinus:posinus`, setgid/default ACL |
| systemd units | `/etc/systemd/system/posinus-*.service` | `root:root`, `0644` |

Один checkout на оба сервиса. Обновление краулера через `update-ubuntu.sh` подтягивает и новый код конвейера, потому что скрипты конвейера работают прямо из `/opt/posinus/pipeline`, без копирования.

SQLite, worker, web-процесс и все процессы прямого доступа к базе должны находиться на одном хосте и локальном диске. NFS, SMB, OneDrive и доступ к файлу базы с другого компьютера запрещены.

## 1. Установить системные пакеты

```bash
sudo apt update
sudo apt install -y \
  acl build-essential curl git lsof pkg-config sqlite3 \
  python3 python3-dev python3-venv \
  libxml2-dev libxslt1-dev zlib1g-dev
python3 --version
```

Версия должна быть `3.12.x`, `3.13.x` или `3.14.x`. На Ubuntu 24.04 штатный `Python 3.12.3` подходит без сторонних репозиториев.

## 2. Создать системного пользователя и группу

```bash
getent group posinus >/dev/null || sudo addgroup --system posinus
id -u posinus >/dev/null 2>&1 || sudo adduser --system \
  --ingroup posinus \
  --home /var/lib/posinus \
  --no-create-home \
  --shell /usr/sbin/nologin \
  posinus
```

Пользователь `posinus` не получает пароль, домашний интерактивный shell или права `sudo`.

## 3. Создать каталоги и общий доступ к SQLite

```bash
sudo install -d -o root -g root -m 0755 /opt/posinus
sudo install -d -o root -g posinus -m 0750 /etc/posinus
sudo install -d -o posinus -g posinus -m 2770 \
  /var/lib/posinus \
  /var/lib/posinus/backups \
  /var/log/posinus

sudo setfacl -m g:posinus:rwx,m:rwx,d:g:posinus:rwx,d:m:rwx \
  /var/lib/posinus \
  /var/lib/posinus/backups \
  /var/log/posinus
```

Режим `2770` сохраняет группу `posinus` у новых файлов, а default ACL поддерживает групповой доступ к каталогу. SQLite создаёт основную базу с исходным режимом `0644`, поэтому после миграции ей отдельно назначается `0660`. WAL/SHM создаются на основе режима основной базы; systemd units дополнительно используют `UMask=0007` и перед стартом нормализуют режим базы.

Проверьте настройки:

```bash
getfacl /var/lib/posinus
getfacl /var/log/posinus
```

## 4. Получить код

Каталог приложения должен изменяться только через `sudo` и скрипт обновления:

```bash
sudo git clone --branch main --single-branch \
  https://github.com/wildcar/posinus.git \
  /opt/posinus
sudo chown -R root:root /opt/posinus
```

Клонируется весь репозиторий, поэтому после этого шага на месте оба сервиса: `/opt/posinus/crawler` и `/opt/posinus/pipeline`.

Если `git clone` сообщает, что каталог не пуст, убедитесь, что это новый хост и `/opt/posinus` не содержит нужных данных. База и секреты в этом каталоге храниться не должны.

## 5. Создать Python-окружение

```bash
sudo python3 -m venv /opt/posinus/crawler/.venv
sudo /opt/posinus/crawler/.venv/bin/python -m pip install --upgrade pip
sudo /opt/posinus/crawler/.venv/bin/python -m pip install -e /opt/posinus/crawler
```

Для отдельного Python используйте, например:

```bash
sudo /usr/bin/python3.13 -m venv /opt/posinus/crawler/.venv
```

## 6. Создать production-конфигурацию

```bash
sudo install -o root -g posinus -m 0640 \
  /opt/posinus/crawler/deploy/crawler.env.example \
  /etc/posinus/crawler.env
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
sudoedit /etc/posinus/crawler.env
```

Вставьте сгенерированное значение в `POSINUS_SECRET_KEY` и обязательно измените:

- `POSINUS_ALLOWED_HOSTS` — домены/IP через запятую;
- `POSINUS_CSRF_TRUSTED_ORIGINS` — полные HTTPS-origin через запятую, если используется reverse proxy;
- email в `POSINUS_USER_AGENT` — действующий технический контакт;
- `POSINUS_SECURE=1` — только после настройки HTTPS.

Для перевода новостей укажите `POSINUS_ROUTER_AUTH_TOKEN`. Это `AUTH_TOKEN` из конфигурации локального `model-router-mcp`. Модель задаёт `POSINUS_TRANSLATION_MODEL`; шаблон использует `deepseek-chat`, как и News Evaluator. Токен остаётся только в `/etc/posinus/crawler.env` с правами `0640`.

Строки файла должны оставаться совместимыми с форматом shell `KEY=value`; значения с пробелами заключайте в двойные кавычки. Пути production менять без необходимости не следует:

```text
POSINUS_DB_PATH=/var/lib/posinus/posinus.sqlite3
POSINUS_BACKUP_DIR=/var/lib/posinus/backups
POSINUS_LOG_DIR=/var/log/posinus
PLAYWRIGHT_BROWSERS_PATH=/var/lib/posinus/playwright
```

Проверьте владельца и права без вывода секрета:

```bash
sudo stat -c '%U:%G %a %n' /etc/posinus/crawler.env
```

Ожидается `root:posinus 640`.

## 7. Установить Chromium для Playwright

```bash
sudo install -d -o root -g posinus -m 2750 /var/lib/posinus/playwright
sudo env PLAYWRIGHT_BROWSERS_PATH=/var/lib/posinus/playwright \
  /opt/posinus/crawler/.venv/bin/python -m playwright install --with-deps chromium
sudo chown -R root:posinus /var/lib/posinus/playwright
sudo chmod -R g+rX,o-rwx /var/lib/posinus/playwright
```

Chromium хранится вне Git-репозитория и доступен сервисному пользователю только на чтение и исполнение.

## 8. Создать схему базы и собрать static files

```bash
sudo install -d -o posinus -g posinus -m 0750 /opt/posinus/crawler/staticfiles
sudo -u posinus /bin/bash -c '
  set -a
  . /etc/posinus/crawler.env
  set +a
  umask 0007
  cd /opt/posinus/crawler
  .venv/bin/python manage.py migrate
  .venv/bin/python manage.py collectstatic --noinput
  .venv/bin/python manage.py check
'
sudo chown posinus:posinus /var/lib/posinus/posinus.sqlite3
sudo chmod 0660 /var/lib/posinus/posinus.sqlite3
sudo chown -R root:root /opt/posinus/crawler/staticfiles
sudo find /opt/posinus/crawler/staticfiles -type d -exec chmod 0755 {} +
sudo find /opt/posinus/crawler/staticfiles -type f -exec chmod 0644 {} +
```

Проверьте базу и права:

```bash
sudo sqlite3 /var/lib/posinus/posinus.sqlite3 'PRAGMA integrity_check;'
sudo stat -c '%U:%G %a %n' /var/lib/posinus/posinus.sqlite3
```

Ожидаются `ok` и `posinus:posinus 660`.

## 9. Создать пользователя веб-интерфейса

Имя ниже относится к Django, а не к системной учётной записи Ubuntu:

```bash
sudo -u posinus /bin/bash -c '
  set -a
  . /etc/posinus/crawler.env
  set +a
  umask 0007
  cd /opt/posinus/crawler
  .venv/bin/python manage.py createoperator crawler-admin
'
```

Команда интерактивно запросит пароль.

## 10. Установить и запустить systemd units

```bash
sudo install -o root -g root -m 0644 \
  /opt/posinus/crawler/deploy/systemd/posinus-web.service \
  /etc/systemd/system/posinus-web.service
sudo install -o root -g root -m 0644 \
  /opt/posinus/crawler/deploy/systemd/posinus-worker.service \
  /etc/systemd/system/posinus-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now posinus-web.service posinus-worker.service
```

Проверка:

```bash
sudo systemctl status --no-pager posinus-web.service posinus-worker.service
curl -I http://127.0.0.1:8000/login/
sudo journalctl -u posinus-web.service -u posinus-worker.service -n 100 --no-pager
```

Waitress слушает только `127.0.0.1:8000`. Не публикуйте его напрямую.

## 11. Опубликовать UI через Nginx и HTTPS

Домен `newscrawler.wildcar.org` должен указывать на production-хост. Установите Nginx и Certbot, затем подключите конфигурацию сайта:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo install -o root -g root -m 0644 \
  /opt/posinus/crawler/deploy/nginx/posinus.conf \
  /etc/nginx/sites-available/posinus
sudo ln -s /etc/nginx/sites-available/posinus \
  /etc/nginx/sites-enabled/posinus
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d newscrawler.wildcar.org --redirect
```

Certbot получит сертификат, добавит TLS в server block и перенаправит HTTP на HTTPS. После успешной выдачи сертификата задайте в `/etc/posinus/crawler.env`:

```text
POSINUS_ALLOWED_HOSTS=127.0.0.1,localhost,newscrawler.wildcar.org
POSINUS_CSRF_TRUSTED_ORIGINS=https://newscrawler.wildcar.org
POSINUS_SECURE=1
```

Перезапустите web-сервис и проверьте внешний endpoint, TLS-сертификат и привязку Waitress:

```bash
sudo systemctl restart posinus-web.service
curl -I https://newscrawler.wildcar.org/login/
sudo certbot renew --dry-run
sudo ss -ltnp | grep ':8000'
```

Ожидается HTTPS-ответ без цикла редиректов; порт 8000 должен остаться привязанным только к `127.0.0.1`. Systemd unit разрешает Waitress принимать `X-Forwarded-Proto` только от loopback-прокси, а Django использует этот заголовок для определения HTTPS; не меняйте адрес Waitress на внешний.

## 12. Дать другим локальным процессам доступ к базе

Для каждого отдельного системного пользователя процесса выполните:

```bash
sudo usermod -aG posinus selector-user
```

Замените `selector-user` реальным именем. После изменения группы перезапустите systemd unit процесса или завершите и начните новую login-сессию. Не добавляйте обычных пользователей без необходимости: член группы может технически изменить любую таблицу SQLite.

Для systemd-процесса рекомендуется явно задать:

```ini
[Service]
SupplementaryGroups=posinus
UMask=0007
Environment=POSINUS_DB_PATH=/var/lib/posinus/posinus.sqlite3
```

Проверка без изменения данных:

```bash
sudo -u selector-user test -r /var/lib/posinus/posinus.sqlite3
sudo -u selector-user test -w /var/lib/posinus/posinus.sqlite3
sudo -u selector-user test -w /var/lib/posinus
sudo -u selector-user sqlite3 /var/lib/posinus/posinus.sqlite3 \
  'SELECT count(*) FROM exchange_news_for_selection;'
```

Все клиенты обязаны устанавливать `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=30000` и быстро завершать транзакции. Подробности записи событий находятся в [contracts/database-contract.md](contracts/database-contract.md).

Ровно один экземпляр `posinus-worker.service` может работать с базой. Дополнительные процессы используют только стабильный `exchange_*` контракт.

## 13. Настроить обновление

Скрипт всегда останавливает web и worker. Если другие systemd-сервисы открывают SQLite, перечислите их по одному в файле:

```bash
sudo install -o root -g root -m 0644 /dev/null /etc/posinus/update-services
sudoedit /etc/posinus/update-services
```

Пример:

```text
posinus-evaluator.service
posinus-preparer.service
posinus-publisher.service
```

Шаг 15 регистрирует эти три unit-а сам. Здесь они как пример и как напоминание: если базу открывает что-то ещё, добавьте его в тот же файл.

Интерактивные процессы нужно завершать вручную. Если после остановки зарегистрированных units база всё ещё открыта, `lsof` остановит обновление до изменения кода или схемы.

Запуск обновления ветки `main`:

```bash
sudo /opt/posinus/crawler/scripts/update-ubuntu.sh
```

Явное имя ветки:

```bash
sudo /opt/posinus/crawler/scripts/update-ubuntu.sh main
```

Скрипт:

1. проверяет root-права, чистоту Git checkout, конфигурацию и список units;
2. получает `origin/main` и запрещает обновление поверх локальных коммитов;
3. запоминает, какие сервисы работали, и останавливает только их;
4. проверяет отсутствие незарегистрированных клиентов SQLite;
5. создаёт integrity-checked backup `pre-update-*.sqlite3`;
6. применяет только fast-forward Git update, зависимости, Chromium и systemd units;
7. выполняет миграции, `collectstatic`, Django check и SQLite integrity check;
8. запускает ранее работавшие сервисы и проверяет web endpoint;
9. при ошибке после остановки сервисов возвращает прежний commit и базу из backup, затем запускает прежние сервисы.

После успешного обновления скрипт выводит старый/новый commit и путь к backup. Проверка:

```bash
cd /opt/posinus
sudo git status --short
sudo git log -1 --oneline
sudo systemctl is-active posinus-web.service posinus-worker.service
sudo sqlite3 /var/lib/posinus/posinus.sqlite3 'PRAGMA integrity_check;'
```

## 14. Диагностика прав доступа

Если клиент получает `attempt to write a readonly database`, проверяйте не только файл базы, но и каталог и sidecar-файлы:

```bash
namei -l /var/lib/posinus/posinus.sqlite3
getfacl /var/lib/posinus
ls -la /var/lib/posinus/posinus.sqlite3*
id selector-user
```

Восстановление ожидаемых прав:

```bash
sudo chown posinus:posinus /var/lib/posinus/posinus.sqlite3
sudo chmod 0660 /var/lib/posinus/posinus.sqlite3
sudo chmod 2770 /var/lib/posinus /var/lib/posinus/backups
sudo setfacl -m g:posinus:rwx,m:rwx,d:g:posinus:rwx,d:m:rwx \
  /var/lib/posinus /var/lib/posinus/backups
```

Не копируйте живой SQLite-файл обычным `cp`. Используйте backup, созданный приложением или скриптом обновления, и останавливайте все прямые клиенты перед ручным восстановлением.

## 15. Установить конвейер оценки и публикации

Краулер к этому моменту должен работать: конвейер читает его базу через группу `posinus` и падает на старте, если `/var/lib/posinus` ещё нет.

Установщик создаёт системного пользователя, поэтому запускает его владелец сервера лично — агентам это правило запрещает.

```bash
sudo bash /opt/posinus/pipeline/deploy/install.sh
```

Скрипт:

1. проверяет, что `/var/lib/posinus` существует и код лежит в `/opt/posinus/pipeline`;
2. создаёт пользователя `posinus-pipeline` в группе `posinus`;
3. создаёт `/var/lib/posinus/pipeline` и `/var/lib/posinus/pipeline/media`;
4. кладёт `/etc/posinus/pipeline.env` из шаблона и подставляет туда токен роутера из `/opt/model-router-mcp/.env`;
5. ставит и включает шесть units: `posinus-evaluator`, `posinus-preparer`, `posinus-publisher` (по service и timer на каждый);
6. регистрирует три service в `/etc/posinus/update-services`, чтобы обновление краулера останавливало их перед миграциями.

Дальше заполните секреты платформ в `/etc/posinus/pipeline.env`. Платформа включается только когда её секрет задан, так что таймер публикации до этого работает впустую и ничего не отправляет. Параметры и повадки каждого сервиса описаны в [../pipeline/docs/services.md](../pipeline/docs/services.md).

Проверка:

```bash
systemctl list-timers 'posinus-*.timer'
sudo journalctl -u posinus-evaluator.service -n 30
sudo systemctl start posinus-evaluator.service
```

Код конвейера обновляется вместе с краулером через `update-ubuntu.sh`: скрипты работают прямо из checkout. Повторно запускать `install.sh` нужно только когда изменились units или шаблон конфигурации.
