# ДЗ 4: хранение данных в БД

Сервис магазина из ДЗ 2 (`hw2/hw/shop_api`) хранит товары и корзины в PostgreSQL через SQLAlchemy 2 (typed ORM, драйвер psycopg 3). HTTP-контракт не изменился. Симуляция проблем изоляции транзакций — в [`isolation_lab/README.md`](isolation_lab/README.md).

## Что сделано

| Путь | Назначение |
|------|------------|
| `hw2/hw/shop_api/models.py` | таблицы `items`, `carts`, `cart_items` |
| `hw2/hw/shop_api/database.py` | engine по `DATABASE_URL` (`pool_pre_ping`), для SQLite — `BEGIN IMMEDIATE` и внешние ключи |
| `hw2/hw/shop_api/store.py` | `ShopStore` поверх БД, каждая операция — одна транзакция |
| `hw2/hw/requirements-runtime.txt` | добавлены `sqlalchemy>=2` и `psycopg[binary]` |
| `hw2/hw/conftest.py` | без `DATABASE_URL` тесты работают на временной SQLite |
| `hw2/hw/test_postgres.py` | тесты на настоящем PostgreSQL в отдельной схеме |
| `docker-compose.yml` | сервис `postgres` (`postgres:17`, healthcheck `pg_isready`, том `pgdata`) |
| `hw4/check_persistence.sh` | проверка, что данные переживают `down`/`up` и `restart` |
| `.github/workflows/hw4-ci.yml` | CI: тесты на SQLite и PostgreSQL (3.13/3.14), проверка сохранности в compose |

Схема создаётся при старте приложения (`metadata.create_all` в lifespan):

- `items(id identity, name, price double precision CHECK (price >= 0), deleted bool DEFAULT false)`;
- `carts(id identity)`;
- `cart_items(cart_id FK, item_id FK, quantity bigint CHECK (quantity > 0), PRIMARY KEY (cart_id, item_id))`.

Транзакции:

- операция целиком внутри `with session.begin()`, любая ошибка откатывает все изменения;
- количество увеличивается одним запросом `INSERT ... ON CONFLICT (cart_id, item_id) DO UPDATE SET quantity = cart_items.quantity + 1`, без чтения и записи по отдельности;
- проверка переполнения цены выполняется в той же транзакции под блокировками строк: добавление берёт `FOR SHARE` на товар и `FOR UPDATE` на корзину, смена цены — `FOR UPDATE` на товар и на корзины с ним (в одном порядке, без взаимных блокировок); при переполнении — 409 и данные не меняются.

Позиции корзины отдаются в порядке `id` товара.

Фильтры `GET /cart`: `min_quantity`/`max_quantity` применяются в SQL (`HAVING` по сумме количеств); значения больше `2**63 - 1` не уходят в БД — слишком большой `min_quantity` сразу даёт `[]`, слишком большой `max_quantity` означает отсутствие верхней границы. Фильтры `min_price`/`max_price` считаются в Python по всем корзинам, прошедшим фильтр по количеству (цена зависит от текущих цен и удалённых товаров), и только потом применяются `offset`/`limit`, поэтому на большом числе корзин такой запрос читает их все.

## Запуск

```bash
docker compose up -d --build
curl -X POST localhost:8080/item -H 'content-type: application/json' -d '{"name": "milk", "price": 1.5}'
docker compose down        # данные остаются в томе pgdata
docker compose down -v     # удалить и данные
```

Проверка сохранности данных (создаёт товар и корзину, делает `docker compose down` без `-v` и `up`, затем `docker compose restart shop postgres` и сравнивает ответы):

```bash
./hw4/check_persistence.sh
docker compose down -v
```

## Тесты

`conftest.py` подставляет изолированную временную SQLite-базу, только если `DATABASE_URL` не задан. Если в окружении экспортирован `DATABASE_URL` из compose, тесты пойдут в эту базу и изменят её данные, поэтому перед запуском тестов выполните `unset DATABASE_URL`:

```bash
cd hw2/hw
pip install -r requirements.txt
unset DATABASE_URL
PYTHONPATH=. pytest test_homework2.py test_shop_extra.py test_metrics.py
```

Тесты на PostgreSQL пропускаются, пока не задан `TEST_POSTGRES_URL`. Каждый тест создаёт свою схему и удаляет её (`DROP SCHEMA ... CASCADE`), данные приложения не затрагиваются:

```bash
docker run -d --rm --name pg-test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=shop_test -p 127.0.0.1:55432:5432 postgres:17
TEST_POSTGRES_URL=postgresql+psycopg://postgres:test@127.0.0.1:55432/shop_test PYTHONPATH=. pytest test_postgres.py -v
docker stop pg-test
```

Они проверяют откат транзакции без частичных записей, отсутствие потерянных обновлений при 8 потоках × 50 добавлений, гонки переполнения цены (одновременные добавления и смены цены), ограничения `CHECK` и внешние ключи, исключение удалённых товаров из цены с пометкой `available: false`.
