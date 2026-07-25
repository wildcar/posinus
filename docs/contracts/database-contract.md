# SQLite exchange contract

Этот контракт предназначен для локального асинхронного отборщика. Миграции Django создают его автоматически.

## Доступ к production-базе на Ubuntu

Production-файл находится в `/var/lib/newscrawler/newscrawler.sqlite3` и должен иметь режим `0660` с группой `newscrawler`. Каждый прямой клиент должен работать на том же хосте под отдельным системным пользователем из этой группы. Каталог имеет setgid/default ACL, а процессы используют `umask 0007`, чтобы SQLite sidecar-файлы `-wal` и `-shm` оставались доступны группе.

SQLite не поддерживает табличные роли: член группы с правом записи технически может изменить любую таблицу. Прикладной контракт разрешает клиентам читать `exchange_news_for_selection`, `exchange_latest_reviews`, `exchange_evaluation_characteristics` и `exchange_latest_evaluation_scores`; добавлять строки можно только в `exchange_review_events` и `exchange_evaluation_scores`.

Перед миграциями или восстановлением базы остановите все прямые клиенты. systemd units таких клиентов перечисляются в `/etc/newscrawler/update-services`; подробности находятся в [ubuntu-deployment.md](ubuntu-deployment.md).

## Чтение очереди

```sql
SELECT n.news_id,
       n.primary_url,
       n.sources_json,
       n.title,
       n.body_text,
       n.language,
       n.published_at,
       n.first_seen_at
FROM exchange_news_for_selection AS n
WHERE NOT EXISTS (
    SELECT 1
    FROM exchange_latest_reviews AS r
    WHERE r.news_id = n.news_id
      AND r.selector_name = :selector_name
)
ORDER BY n.first_seen_at
LIMIT :batch_size;
```

`sources_json` — JSON-массив объектов с `url`, `canonical_url`, `source_id`, `source_name`, `domain` и `fetched_at`.

## Запись решения

```sql
INSERT INTO exchange_review_events (
    news_id, decision, score, reason,
    selector_name, selector_version,
    idempotency_key, created_at
) VALUES (
    :news_id, :decision, :score, :reason,
    :selector_name, :selector_version,
    :idempotency_key, :created_at
);
```

- `decision`: `positive`, `not_positive` или `skipped`;
- `score`: `NULL` или `[0, 1]`;
- `idempotency_key`: стабильный уникальный идентификатор попытки;
- `created_at`: UTC ISO 8601, например `2026-07-13T12:30:00+00:00`.

Транзакция должна содержать небольшой batch и сразу завершаться `COMMIT`. При `database is locked` следует повторить транзакцию с экспоненциальной задержкой. Не удерживайте read-транзакцию во время работы модели.

## Набор характеристик оценки

Сервис-оценщик работает с фиксированным набором характеристик (v1, 20 осей). Набор хранится в `exchange_evaluation_characteristics`, заполняется миграциями краулера; клиенты только читают его.

```sql
SELECT key, category, title, description,
       anchor_low, anchor_high, threshold_direction, position
FROM exchange_evaluation_characteristics
ORDER BY position;
```

- Каждая ось оценивается целым числом от 0 до 10; 0 значит «признак отсутствует или неприменим», штрафом не является.
- Оси независимы: `negativity` не инверсия `positivity`, суммироваться в фиксированную величину оценки не обязаны.
- `anchor_low` и `anchor_high` описывают смысл значений 0 и 10.
- `threshold_direction` задаёт направление порога. `upper_bound` значит «не выше N» (`negativity`, `clickbait`, `controversy`, `promo`), `lower_bound` значит «не ниже N» (остальные 16 осей).

## Запись оценок

Оценки привязываются к событию решения. В одной транзакции вставьте событие в `exchange_review_events`, получите его `id` (`RETURNING id` или `last_insert_rowid()`) и добавьте по строке на каждую ось:

```sql
INSERT INTO exchange_evaluation_scores (review_event_id, characteristic_key, value)
VALUES (:review_event_id, :characteristic_key, :value);
```

- `value` - целое от 0 до 10, диапазон проверяется CHECK-ограничением.
- `characteristic_key` должен существовать в `exchange_evaluation_characteristics`; включите `PRAGMA foreign_keys = ON`, чтобы SQLite проверял ссылку на вашем соединении.
- Пара `(review_event_id, characteristic_key)` уникальна: одна оценка на ось в рамках события.
- `UPDATE` и `DELETE` запрещены триггерами.
- Однострочный комментарий оценщика пишите в поле `reason` события.

## Чтение последних оценок

`exchange_latest_evaluation_scores` возвращает оценки последнего события по паре новости и имени оценщика. Столбцы `news_id`, `selector_name`, `review_event_id`, `created_at`, `characteristic_key`, `value`.

```sql
SELECT characteristic_key, value
FROM exchange_latest_evaluation_scores
WHERE news_id = :news_id
  AND selector_name = :selector_name;
```

## Исправление решения

События и оценки неизменяемы. Для исправления вставьте новое событие с другим `idempotency_key` и полным набором оценок, если ваш сервис их проставляет. `exchange_latest_reviews` и `exchange_latest_evaluation_scores` выберут его по `created_at`, затем по `id`.

## Ручной отбор для настройки весов

Кнопка «Отобрано» создаёт положительное событие с именем отборщика `operator:<имя>` и копирует в него последние баллы настроенного автоматического отборщика. Повторное нажатие не добавляет второе событие. Для обучающей выборки соедините ручные события с `exchange_evaluation_scores` по идентификатору события, а ссылки возьмите из `exchange_news_for_selection` по `news_id`.
