# SQLite exchange contract

Этот контракт предназначен для локального асинхронного отборщика. Миграции Django создают его автоматически.

## Доступ к production-базе на Ubuntu

Production-файл находится в `/var/lib/posinus/posinus.sqlite3` и должен иметь режим `0660` с группой `posinus`. Каждый прямой клиент должен работать на том же хосте под отдельным системным пользователем из этой группы. Каталог имеет setgid/default ACL, а процессы используют `umask 0007`, чтобы SQLite sidecar-файлы `-wal` и `-shm` оставались доступны группе.

SQLite не поддерживает табличные роли: член группы с правом записи технически может изменить любую таблицу. Прикладной контракт разрешает клиентам читать `exchange_news_for_selection`, `exchange_latest_reviews`, `exchange_evaluation_characteristics`, `exchange_latest_evaluation_scores`, `exchange_active_selection_profile`, `exchange_topic`, `exchange_publication_order` и `exchange_daypic_slot`; добавлять строки можно только в `exchange_review_events` и `exchange_evaluation_scores`, а в `exchange_news_topic` разрешена ещё и правка своей строки (по строке на новость, см. «Тема новости»).

Перед миграциями или восстановлением базы остановите все прямые клиенты. systemd units таких клиентов перечисляются в `/etc/posinus/update-services`; подробности находятся в [deployment.md](../deployment.md).

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

## Тема новости

Двадцать осей говорят, насколько новость хороша, и ни одна не замечает, что это третья спасённая собака за неделю. Однообразие ленты видно только по составу, поэтому у новости есть рубрика из закрытого списка. Список владеют миграции краулера, таблица `exchange_topic`: столбцы `key`, `title`, `description`, `assignable`, `position`.

```sql
SELECT key, title, description
FROM exchange_topic
WHERE assignable = 1
ORDER BY position;
```

`assignable = 0` только у заглушки `unknown`. Предлагать её модели нельзя: это то, что клиент ставит сам, когда ответ не подошёл, и то, что несёт корпус, оценённый до появления рубрик.

Рубрику пишут в `exchange_news_topic` в той же транзакции, что и событие решения:

```sql
INSERT INTO exchange_news_topic (news_id, topic_key, selector_name, selector_version, created_at)
VALUES (:news_id, :topic_key, :selector_name, :selector_version, :created_at)
ON CONFLICT(news_id) DO UPDATE SET
    topic_key = excluded.topic_key,
    selector_name = excluded.selector_name,
    selector_version = excluded.selector_version,
    created_at = excluded.created_at;
```

По строке на новость, и это единственное место в контракте, где разрешён `UPDATE`. Событие решения неизменяемо потому, что по нему потом объясняют старое решение; рубрика же описывает саму новость, а не решение о ней, и исправлять в ней нечего, кроме ошибки.

Два правила для клиента. Пустой список рубрик или отсутствие таблицы — не ошибка: работайте как раньше, ничего не спрашивая у модели и ничего не записывая. И `topic_key` вне списка контракт отвергнет внешним ключом, а вместе с ним откатится всё событие — сверяйте ключ до записи и ставьте `unknown`, когда ответ не подошёл.

## Чтение действующего профиля отбора

Пороги, по которым баллы превращаются в решение, лежат в базе краулера, а не в коде отборщика: правило должно быть одно на двоих, иначе интерфейс объясняет решения по своей копии и через месяц копии расходятся. Владеют таблицами миграции краулера, клиент читает одну вьюху `exchange_active_selection_profile` - по строке на порог действующего профиля.

Столбцы: `profile_name`, `profile_revision`, `characteristic_key`, `kind`, `value`. Виды порога:

| `kind` | Смысл |
|---|---|
| `gate_min` | обязательное условие: балл не ниже `value` |
| `gate_max` | обязательное условие: балл не выше `value` |
| `highlight_min` | сильная сторона: достаточно одной оси с баллом не ниже `value` |

```sql
SELECT profile_name, profile_revision, characteristic_key, kind, value
FROM exchange_active_selection_profile;
```

Границы включительные, отсутствующая у новости ось читается как 0. Новость отбирается, когда выполнены все обязательные условия и хотя бы одна сильная сторона дотянула до своего порога; если сильных сторон в профиле нет, достаточно обязательных.

Два правила для клиента. Пустой ответ - это не «профиль без условий», а «профиля нет»: используйте зашитый запасной профиль и напишите об этом в журнал, иначе отбор пропустит вообще всё. И записывайте `profile_name` с `profile_revision` в `selector_version` события (`0.2.0+deepseek-v4-pro+default.r3`): таблица порогов изменяемая, а события нет, и без этого через полгода будет нечем объяснить старое решение.

Смена порогов не пересчитывает уже оценённое: очередь исключает новости, у которых уже есть событие от этого имени. Пересчёт - это отдельный проход по сохранённым баллам с записью исправляющих событий.

## Чтение порядка публикации

Порядок, в котором подготовленные новости уходят в эфир, задаёт краулер: у него есть и оценки, и правки оператора. Клиент читает вьюху `exchange_publication_order` - по строке на оценённую новость.

Столбцы: `news_id`, `strength`, `operator_rank`, `hold_until`, `dropped_at`.

`strength` - сила новости от 0 до 10: половина её это лучшая из семи сильных сторон, остальное позитивность и интересность. Время подготовки для порядка худший из признаков: оно говорит, когда до новости дошли руки у машины, а не насколько новость хороша.

`operator_rank` меньше нуля - оператор поднял новость, больше нуля - опустил, ноль - решает сила. `hold_until` в будущем означает «отложена», непустой `dropped_at` - «снята с очереди»; и то и другое клиент обязан пропускать.

```sql
SELECT news_id, strength, operator_rank, hold_until, dropped_at
FROM exchange_publication_order;
```

Сортировка: `operator_rank`, затем `strength` по убыванию, затем свой прежний порядок как устойчивый третий ключ. Пустой ответ или отсутствие вьюхи - не ошибка: клиент работает как раньше, по времени подготовки.

## Настройки «Картины дня»

«Картина дня» — ежедневная сгенерированная картинка, которую конвейер публикует на площадки. Её настройки правит оператор в интерфейсе краулера, а исполняет конвейер, поэтому они лежат здесь — по той же причине, что и пороги отбора: правило одно на двоих. Таблицей владеют миграции краулера, клиент её только читает; сами картинки, журнал генераций и результаты публикаций живут в базе конвейера, интерфейс читает их оттуда.

Таблица `exchange_daypic_slot`, по строке на выпуск («слот»): `day` — картина дня, позже добавится вечерний слот.

```sql
SELECT slot, enabled, title, generate_at,
       prompt, system_prompt, styles,
       chat_provider, chat_model, image_provider, image_model,
       image_size, image_size_wide
FROM exchange_daypic_slot
WHERE enabled = 1;
```

- `slot` — ключ выпуска (`day`, `evening`, …), латиницей.
- `enabled` — выключенный слот клиент пропускает молча.
- `title` — подпись публикации («Картина дня»).
- `generate_at` — местное время `HH:MM` (зону задаёт конфиг клиента, по умолчанию Москва), раньше которого выпуск за сегодня не делается.
- `prompt` и `system_prompt` — задание чат-модели, которая пишет промпт для генерации изображения.
- `styles` — список стилей, по одному на строку; клиент выбирает стиль случайно, не повторяя уже использованные этим слотом в текущем месяце (когда неиспользованных не осталось, берётся любой).
- `chat_provider`/`chat_model`, `image_provider`/`image_model` — подсказки роутеру; пустое значение оставляет выбор клиенту или роутеру.
- `image_size` и `image_size_wide` — размеры вертикальной и горизонтальной картинок: выпуск рисуется в двух ориентациях (вертикальная уходит в telegram, горизонтальная — на сайты и в VK).

Два правила для клиента. Отсутствие таблицы или пустой ответ — не ошибка: «Картина дня» просто не настроена, работайте дальше без неё. И правок в этой таблице у клиента нет: настройка принадлежит оператору, результат — конвейеру.

## Исправление решения

События и оценки неизменяемы. Для исправления вставьте новое событие с другим `idempotency_key` и полным набором оценок, если ваш сервис их проставляет. `exchange_latest_reviews` и `exchange_latest_evaluation_scores` выберут его по `created_at`, затем по `id`.

## Ручной отбор для настройки весов

Кнопка «Отобрано» создаёт положительное событие с именем отборщика `operator:<имя>` и копирует в него последние баллы настроенного автоматического отборщика. Повторное нажатие не добавляет второе событие. Для обучающей выборки соедините ручные события с `exchange_evaluation_scores` по идентификатору события, а ссылки возьмите из `exchange_news_for_selection` по `news_id`.
