# Развёртывание Positive News Crawler на Ubuntu

Инструкция рассчитана на чистый односерверный production-хост и запуск команд из обычной учётной записи с правами `sudo`. Имя этой учётной записи не используется в путях и правах. Приложение работает под отдельным системным пользователем `newscrawler` без интерактивного входа.

Основной вариант — Ubuntu 24.04 LTS со штатными Python 3.12 и пакетом [python3-venv](https://packages.ubuntu.com/noble/python/python3-venv). Также поддерживаются Python 3.13 и 3.14 на более новых Ubuntu. Не заменяйте системный `/usr/bin/python3` вручную.

## Итоговая структура

| Назначение | Путь | Владелец и доступ |
|---|---|---|
| Код и virtualenv | `/opt/newscrawler` | `root:root`, сервисы только читают |
| Production-конфигурация | `/etc/newscrawler/newscrawler.env` | `root:newscrawler`, `0640` |
| SQLite, backup, Chromium | `/var/lib/newscrawler` | группа `newscrawler`, setgid/default ACL |
| Основная база | `/var/lib/newscrawler/newscrawler.sqlite3` | `newscrawler:newscrawler`, `0660` |
| Логи приложения | `/var/log/newscrawler` | `newscrawler:newscrawler`, setgid/default ACL |
| systemd units | `/etc/systemd/system/newscrawler-*.service` | `root:root`, `0644` |

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
getent group newscrawler >/dev/null || sudo addgroup --system newscrawler
id -u newscrawler >/dev/null 2>&1 || sudo adduser --system \
  --ingroup newscrawler \
  --home /var/lib/newscrawler \
  --no-create-home \
  --shell /usr/sbin/nologin \
  newscrawler
```

Пользователь `newscrawler` не получает пароль, домашний интерактивный shell или права `sudo`.

## 3. Создать каталоги и общий доступ к SQLite

```bash
sudo install -d -o root -g root -m 0755 /opt/newscrawler
sudo install -d -o root -g newscrawler -m 0750 /etc/newscrawler
sudo install -d -o newscrawler -g newscrawler -m 2770 \
  /var/lib/newscrawler \
  /var/lib/newscrawler/backups \
  /var/log/newscrawler

sudo setfacl -m g:newscrawler:rwx,m:rwx,d:g:newscrawler:rwx,d:m:rwx \
  /var/lib/newscrawler \
  /var/lib/newscrawler/backups \
  /var/log/newscrawler
```

Режим `2770` сохраняет группу `newscrawler` у новых файлов, а default ACL поддерживает групповой доступ к каталогу. SQLite создаёт основную базу с исходным режимом `0644`, поэтому после миграции ей отдельно назначается `0660`. WAL/SHM создаются на основе режима основной базы; systemd units дополнительно используют `UMask=0007` и перед стартом нормализуют режим базы.

Проверьте настройки:

```bash
getfacl /var/lib/newscrawler
getfacl /var/log/newscrawler
```

## 4. Получить код

Каталог приложения должен изменяться только через `sudo` и скрипт обновления:

```bash
sudo git clone --branch main --single-branch \
  https://github.com/wildcar/positive-news-crawler.git \
  /opt/newscrawler
sudo chown -R root:root /opt/newscrawler
```

Если `git clone` сообщает, что каталог не пуст, убедитесь, что это новый хост и `/opt/newscrawler` не содержит нужных данных. База и секреты в этом каталоге храниться не должны.

## 5. Создать Python-окружение

```bash
sudo python3 -m venv /opt/newscrawler/.venv
sudo /opt/newscrawler/.venv/bin/python -m pip install --upgrade pip
sudo /opt/newscrawler/.venv/bin/python -m pip install -e /opt/newscrawler
```

Для отдельного Python используйте, например:

```bash
sudo /usr/bin/python3.13 -m venv /opt/newscrawler/.venv
```

## 6. Создать production-конфигурацию

```bash
sudo install -o root -g newscrawler -m 0640 \
  /opt/newscrawler/deploy/newscrawler.env.example \
  /etc/newscrawler/newscrawler.env
python3 -c 'import secrets; print(secrets.token_urlsafe(64))'
sudoedit /etc/newscrawler/newscrawler.env
```

Вставьте сгенерированное значение в `NEWSCRAWLER_SECRET_KEY` и обязательно измените:

- `NEWSCRAWLER_ALLOWED_HOSTS` — домены/IP через запятую;
- `NEWSCRAWLER_CSRF_TRUSTED_ORIGINS` — полные HTTPS-origin через запятую, если используется reverse proxy;
- email в `NEWSCRAWLER_USER_AGENT` — действующий технический контакт;
- `NEWSCRAWLER_SECURE=1` — только после настройки HTTPS.

Для перевода новостей укажите `NEWSCRAWLER_ROUTER_AUTH_TOKEN`. Это `AUTH_TOKEN` из конфигурации локального `model-router-mcp`. Модель задаёт `NEWSCRAWLER_TRANSLATION_MODEL`; шаблон использует `deepseek-chat`, как и News Evaluator. Токен остаётся только в `/etc/newscrawler/newscrawler.env` с правами `0640`.

Строки файла должны оставаться совместимыми с форматом shell `KEY=value`; значения с пробелами заключайте в двойные кавычки. Пути production менять без необходимости не следует:

```text
NEWSCRAWLER_DB_PATH=/var/lib/newscrawler/newscrawler.sqlite3
NEWSCRAWLER_BACKUP_DIR=/var/lib/newscrawler/backups
NEWSCRAWLER_LOG_DIR=/var/log/newscrawler
PLAYWRIGHT_BROWSERS_PATH=/var/lib/newscrawler/playwright
```

Проверьте владельца и права без вывода секрета:

```bash
sudo stat -c '%U:%G %a %n' /etc/newscrawler/newscrawler.env
```

Ожидается `root:newscrawler 640`.

## 7. Установить Chromium для Playwright

```bash
sudo install -d -o root -g newscrawler -m 2750 /var/lib/newscrawler/playwright
sudo env PLAYWRIGHT_BROWSERS_PATH=/var/lib/newscrawler/playwright \
  /opt/newscrawler/.venv/bin/python -m playwright install --with-deps chromium
sudo chown -R root:newscrawler /var/lib/newscrawler/playwright
sudo chmod -R g+rX,o-rwx /var/lib/newscrawler/playwright
```

Chromium хранится вне Git-репозитория и доступен сервисному пользователю только на чтение и исполнение.

## 8. Создать схему базы и собрать static files

```bash
sudo install -d -o newscrawler -g newscrawler -m 0750 /opt/newscrawler/staticfiles
sudo -u newscrawler /bin/bash -c '
  set -a
  . /etc/newscrawler/newscrawler.env
  set +a
  umask 0007
  cd /opt/newscrawler
  .venv/bin/python manage.py migrate
  .venv/bin/python manage.py collectstatic --noinput
  .venv/bin/python manage.py check
'
sudo chown newscrawler:newscrawler /var/lib/newscrawler/newscrawler.sqlite3
sudo chmod 0660 /var/lib/newscrawler/newscrawler.sqlite3
sudo chown -R root:root /opt/newscrawler/staticfiles
sudo find /opt/newscrawler/staticfiles -type d -exec chmod 0755 {} +
sudo find /opt/newscrawler/staticfiles -type f -exec chmod 0644 {} +
```

Проверьте базу и права:

```bash
sudo sqlite3 /var/lib/newscrawler/newscrawler.sqlite3 'PRAGMA integrity_check;'
sudo stat -c '%U:%G %a %n' /var/lib/newscrawler/newscrawler.sqlite3
```

Ожидаются `ok` и `newscrawler:newscrawler 660`.

## 9. Создать пользователя веб-интерфейса

Имя ниже относится к Django, а не к системной учётной записи Ubuntu:

```bash
sudo -u newscrawler /bin/bash -c '
  set -a
  . /etc/newscrawler/newscrawler.env
  set +a
  umask 0007
  cd /opt/newscrawler
  .venv/bin/python manage.py createoperator crawler-admin
'
```

Команда интерактивно запросит пароль.

## 10. Установить и запустить systemd units

```bash
sudo install -o root -g root -m 0644 \
  /opt/newscrawler/deploy/systemd/newscrawler-web.service \
  /etc/systemd/system/newscrawler-web.service
sudo install -o root -g root -m 0644 \
  /opt/newscrawler/deploy/systemd/newscrawler-worker.service \
  /etc/systemd/system/newscrawler-worker.service
sudo systemctl daemon-reload
sudo systemctl enable --now newscrawler-web.service newscrawler-worker.service
```

Проверка:

```bash
sudo systemctl status --no-pager newscrawler-web.service newscrawler-worker.service
curl -I http://127.0.0.1:8000/login/
sudo journalctl -u newscrawler-web.service -u newscrawler-worker.service -n 100 --no-pager
```

Waitress слушает только `127.0.0.1:8000`. Не публикуйте его напрямую.

## 11. Опубликовать UI через Nginx и HTTPS

Домен `newscrawler.wildcar.org` должен указывать на production-хост. Установите Nginx и Certbot, затем подключите конфигурацию сайта:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo install -o root -g root -m 0644 \
  /opt/newscrawler/deploy/nginx/newscrawler.conf \
  /etc/nginx/sites-available/newscrawler
sudo ln -s /etc/nginx/sites-available/newscrawler \
  /etc/nginx/sites-enabled/newscrawler
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d newscrawler.wildcar.org --redirect
```

Certbot получит сертификат, добавит TLS в server block и перенаправит HTTP на HTTPS. После успешной выдачи сертификата задайте в `/etc/newscrawler/newscrawler.env`:

```text
NEWSCRAWLER_ALLOWED_HOSTS=127.0.0.1,localhost,newscrawler.wildcar.org
NEWSCRAWLER_CSRF_TRUSTED_ORIGINS=https://newscrawler.wildcar.org
NEWSCRAWLER_SECURE=1
```

Перезапустите web-сервис и проверьте внешний endpoint, TLS-сертификат и привязку Waitress:

```bash
sudo systemctl restart newscrawler-web.service
curl -I https://newscrawler.wildcar.org/login/
sudo certbot renew --dry-run
sudo ss -ltnp | grep ':8000'
```

Ожидается HTTPS-ответ без цикла редиректов; порт 8000 должен остаться привязанным только к `127.0.0.1`. Systemd unit разрешает Waitress принимать `X-Forwarded-Proto` только от loopback-прокси, а Django использует этот заголовок для определения HTTPS; не меняйте адрес Waitress на внешний.

## 12. Дать другим локальным процессам доступ к базе

Для каждого отдельного системного пользователя процесса выполните:

```bash
sudo usermod -aG newscrawler selector-user
```

Замените `selector-user` реальным именем. После изменения группы перезапустите systemd unit процесса или завершите и начните новую login-сессию. Не добавляйте обычных пользователей без необходимости: член группы может технически изменить любую таблицу SQLite.

Для systemd-процесса рекомендуется явно задать:

```ini
[Service]
SupplementaryGroups=newscrawler
UMask=0007
Environment=NEWSCRAWLER_DB_PATH=/var/lib/newscrawler/newscrawler.sqlite3
```

Проверка без изменения данных:

```bash
sudo -u selector-user test -r /var/lib/newscrawler/newscrawler.sqlite3
sudo -u selector-user test -w /var/lib/newscrawler/newscrawler.sqlite3
sudo -u selector-user test -w /var/lib/newscrawler
sudo -u selector-user sqlite3 /var/lib/newscrawler/newscrawler.sqlite3 \
  'SELECT count(*) FROM exchange_news_for_selection;'
```

Все клиенты обязаны устанавливать `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=30000` и быстро завершать транзакции. Подробности записи событий находятся в [database-contract.md](database-contract.md).

Ровно один экземпляр `newscrawler-worker.service` может работать с базой. Дополнительные процессы используют только стабильный `exchange_*` контракт.

## 13. Настроить обновление

Скрипт всегда останавливает web и worker. Если другие systemd-сервисы открывают SQLite, перечислите их по одному в файле:

```bash
sudo install -o root -g root -m 0644 /dev/null /etc/newscrawler/update-services
sudoedit /etc/newscrawler/update-services
```

Пример:

```text
positive-selector.service
report-exporter.service
```

Интерактивные процессы нужно завершать вручную. Если после остановки зарегистрированных units база всё ещё открыта, `lsof` остановит обновление до изменения кода или схемы.

Запуск обновления ветки `main`:

```bash
sudo /opt/newscrawler/scripts/update-ubuntu.sh
```

Явное имя ветки:

```bash
sudo /opt/newscrawler/scripts/update-ubuntu.sh main
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
cd /opt/newscrawler
sudo git status --short
sudo git log -1 --oneline
sudo systemctl is-active newscrawler-web.service newscrawler-worker.service
sudo sqlite3 /var/lib/newscrawler/newscrawler.sqlite3 'PRAGMA integrity_check;'
```

## 14. Диагностика прав доступа

Если клиент получает `attempt to write a readonly database`, проверяйте не только файл базы, но и каталог и sidecar-файлы:

```bash
namei -l /var/lib/newscrawler/newscrawler.sqlite3
getfacl /var/lib/newscrawler
ls -la /var/lib/newscrawler/newscrawler.sqlite3*
id selector-user
```

Восстановление ожидаемых прав:

```bash
sudo chown newscrawler:newscrawler /var/lib/newscrawler/newscrawler.sqlite3
sudo chmod 0660 /var/lib/newscrawler/newscrawler.sqlite3
sudo chmod 2770 /var/lib/newscrawler /var/lib/newscrawler/backups
sudo setfacl -m g:newscrawler:rwx,m:rwx,d:g:newscrawler:rwx,d:m:rwx \
  /var/lib/newscrawler /var/lib/newscrawler/backups
```

Не копируйте живой SQLite-файл обычным `cp`. Используйте backup, созданный приложением или скриптом обновления, и останавливайте все прямые клиенты перед ручным восстановлением.
