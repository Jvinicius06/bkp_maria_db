#!/usr/bin/env bash
# Restaura um backup .sql.gz no MariaDB.
# Uso:
#   docker compose exec mariadb-backup /app/scripts/restore.sh /backups/arquivo.sql.gz [db_destino]
#   docker compose exec mariadb-backup /app/scripts/restore.sh gdrive:backups/mariadb/arquivo.sql.gz
set -euo pipefail

: "${DB_HOST:?}"; : "${DB_PORT:=3306}"; : "${DB_USER:?}"; : "${DB_PASSWORD:?}"

SRC="${1:?arquivo .sql.gz obrigatório (local ou remote rclone)}"
TARGET_DB="${2:-${DB_NAME}}"

TMPFILE=""
cleanup() { [[ -n "$TMPFILE" && -f "$TMPFILE" ]] && rm -f "$TMPFILE"; }
trap cleanup EXIT

# Se for remote do rclone (contém ':' antes de '/'), baixa primeiro
if [[ "$SRC" == *:* && "$SRC" != /* ]]; then
    TMPFILE="/tmp/restore-$(date +%s).sql.gz"
    echo "Baixando ${SRC} -> ${TMPFILE}"
    rclone copyto "$SRC" "$TMPFILE"
    SRC="$TMPFILE"
fi

[[ -f "$SRC" ]] || { echo "Arquivo não encontrado: $SRC"; exit 1; }

echo "ATENÇÃO: vai restaurar $SRC no banco '${TARGET_DB}' em ${DB_HOST}:${DB_PORT}"
echo "Pressione Ctrl+C em 5s para cancelar..."
sleep 5

gunzip -c "$SRC" \
  | mariadb \
        --host="$DB_HOST" --port="$DB_PORT" \
        --user="$DB_USER" --password="$DB_PASSWORD" \
        --default-character-set=utf8mb4 \
        "$TARGET_DB"

echo "Restore concluído."
