# posinus

Машина позитивных новостей. Собирает публичные новости, оценивает каждую по двадцати
характеристикам, отбирает сильные, пересказывает их по-русски и публикует.

Результат выходит в Telegram-канале [@posinus](https://t.me/posinus), в блоге «Позитивные
новости» на wildcar.ru и на стене ВК-сообщества [@positivenus](https://vk.com/positivenus).

## Два сервиса

| Каталог | Что делает | Стек |
|---|---|---|
| [crawler/](crawler/) | Собирает статьи в локальную SQLite, склеивает перепечатки, ведёт список источников, даёт оператору веб-интерфейс | Django 5.2, Playwright, venv |
| [pipeline/](pipeline/) | Оценивает, отбирает, пересказывает и публикует: `evaluator.py` → `preparer.py` → `publisher.py` | Python 3.12, только stdlib |

Между ними ровно один интерфейс: SQL-контракт `exchange_*` в одном файле SQLite,
описанный в [docs/contracts/database-contract.md](docs/contracts/database-contract.md).
Схему контракта создают миграции краулера, конвейер её только читает и дописывает два
журнала. Ни общего кода, ни импортов через границу.

## Как это работает

```text
источники --> crawler: обход, извлечение текста, дедупликация
                 |
                 v  exchange_news_for_selection
          evaluator: 20 оценок 0-10 через модель, профиль отбора решает
                 |
                 v  positive
          preparer: перевыкачка статьи, иллюстрации, пересказ в markdown
                 |
                 v
          publisher: Telegram, wildcar.ru, ВК
```

Всё живёт на одном хосте: один файл базы на локальном диске, один worker краулера,
три systemd-таймера конвейера.

## Начать

```bash
cd crawler && sh scripts/install.sh          # Django-сервис: venv, зависимости, миграции
cd pipeline && python3 -m unittest discover -s tests   # конвейеру ставить нечего
```

Подробности по сервисам в [crawler/README.md](crawler/README.md) и
[pipeline/README.md](pipeline/README.md). Production-развёртывание на Ubuntu, включая
системных пользователей, права на общую базу и обновление, описано в
[docs/deployment.md](docs/deployment.md).

## Агентам

Начинайте с [AGENTS.md](AGENTS.md): там границы сервисов, общие правила и указатель, какие
документы читать под конкретную задачу. Задача, которая цепляет оба сервиса, требует
прочитать состояние обоих.
