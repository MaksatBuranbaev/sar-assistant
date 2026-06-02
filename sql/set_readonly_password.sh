#!/bin/sh
# Выставляет пароль роли pss_readonly из переменной окружения.
#
# Зачем отдельный скрипт: sql/readonly_role.sql создаёт роль с паролем-заглушкой,
# потому что в .sql файл переменную окружения не подставить. В compose пароль
# должен задаваться снаружи и одним значением для БД и для приложения — иначе
# они разъезжаются, и приложение получает "password authentication failed".
#
# Запускается автоматически как часть docker-entrypoint-initdb.d (последним).

set -e

: "${PSS_READONLY_PASSWORD:?переменная PSS_READONLY_PASSWORD не задана}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    ALTER ROLE pss_readonly PASSWORD '${PSS_READONLY_PASSWORD}';
EOSQL

echo "пароль роли pss_readonly установлен"
