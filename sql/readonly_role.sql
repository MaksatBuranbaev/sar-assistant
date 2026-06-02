-- readonly_role.sql — роль, под которой работает ассистент.
--
-- Это главный слой защиты от SQL, сгенерированного языковой моделью.
-- Валидатор запросов (sql_guard.py) можно обойти — он разбирает текст.
-- Права в СУБД обойти нельзя: если роли не выдан DELETE, DELETE не выполнится
-- независимо от того, что и как модель сгенерировала.
--
-- Запускать под суперпользователем:
--     psql -U postgres -d pss_db -f sql/readonly_role.sql

\set ON_ERROR_STOP on

-- 1. Роль без права создавать объекты и без наследования лишних привилегий.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pss_readonly') THEN
        CREATE ROLE pss_readonly LOGIN PASSWORD 'change_me_in_env' NOCREATEDB NOCREATEROLE NOSUPERUSER;
    END IF;
END
$$;

-- 2. Сначала отбираем всё, потом выдаём точечно. Порядок важен: GRANT поверх
--    невыясненного состояния прав оставляет дыры.
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM pss_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM pss_readonly;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM pss_readonly;
REVOKE ALL ON SCHEMA public FROM pss_readonly;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 3. Минимально необходимое.
--    USAGE на схему обязателен: без него SELECT не пройдёт даже при выданном
--    GRANT SELECT на таблицу — это самая частая ошибка при настройке такой роли.
GRANT CONNECT ON DATABASE pss_db TO pss_readonly;
GRANT USAGE   ON SCHEMA public   TO pss_readonly;
GRANT SELECT  ON pss_departures, pss_lessons TO pss_readonly;

-- 4. Новые таблицы НЕ должны становиться доступны автоматически.
--    Поэтому ALTER DEFAULT PRIVILEGES здесь намеренно не используется:
--    появится таблица с персональными данными — ассистент её не увидит,
--    пока доступ не выдадут явно.

-- 5. Ограничения уровня роли. Действуют для всех подключений этой роли,
--    даже если приложение забудет выставить их у себя.
ALTER ROLE pss_readonly SET default_transaction_read_only = on;
ALTER ROLE pss_readonly SET statement_timeout = '8s';
ALTER ROLE pss_readonly SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE pss_readonly SET lock_timeout = '2s';

-- 6. Чего этой ролью закрыть НЕЛЬЗЯ.
--    Системные каталоги (pg_catalog, information_schema) в PostgreSQL доступны
--    роли PUBLIC на чтение, и отобрать это, не сломав работу клиентов, нельзя:
--    сам psycopg читает каталог при подключении. Поэтому запрет на обращение
--    к pg_catalog реализован в sql_guard.py на уровне разбора запроса.
--    Ровно для таких случаев защита и делается многослойной: то, что не
--    закрывается правами, закрывается валидатором, и наоборот.

-- Проверка результата:
--   SET ROLE pss_readonly;
--   SELECT count(*) FROM pss_departures;      -- работает
--   DELETE FROM pss_departures;               -- ERROR: permission denied
--   CREATE TABLE t (id int);                  -- ERROR: permission denied
--   RESET ROLE;
