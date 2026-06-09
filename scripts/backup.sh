#!/usr/bin/env bash
# Backup MariaDB com baixo impacto + upload para Google Drive via rclone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify.sh"

: "${DB_HOST:?DB_HOST obrigatório}"
: "${DB_PORT:=3306}"
: "${DB_USER:?DB_USER obrigatório}"
: "${DB_PASSWORD:?DB_PASSWORD obrigatório}"
: "${DB_NAME:?DB_NAME obrigatório}"
: "${RCLONE_REMOTE:?RCLONE_REMOTE obrigatório (ex: gdrive:backups/mariadb)}"
: "${BACKUP_DIR:=/backups}"
: "${THROTTLE_MBPS:=5}"
: "${GZIP_LEVEL:=1}"
: "${LOCAL_RETENTION:=3}"
: "${REMOTE_RETENTION:=72}"

mkdir -p "$BACKUP_DIR"

TS=$(date +%Y%m%d-%H%M%S)
BASENAME="${DB_NAME}-${TS}.sql.gz"
OUTFILE="${BACKUP_DIR}/${BASENAME}"
LOGFILE="${BACKUP_DIR}/backup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOGFILE"; }

# fail <exit_code> <titulo_discord> <mensagem>
# Loga, notifica o Discord e encerra.
fail() {
    local code="$1" title="$2" msg="$3"
    log "${msg}"
    notify_discord error "${title}" \
"${msg}

Servidor DB: ${DB_HOST}:${DB_PORT} · Banco: ${DB_NAME}"
    exit "${code}"
}

log "=== Iniciando backup de '${DB_NAME}' em ${DB_HOST}:${DB_PORT} ==="
log "Destino local: ${OUTFILE}"
log "Throttle: ${THROTTLE_MBPS} MB/s | gzip: -${GZIP_LEVEL}"

START=$(date +%s)

# Testa conexão antes de começar (falha rápida)
if ! mariadb \
        --host="$DB_HOST" --port="$DB_PORT" \
        --user="$DB_USER" --password="$DB_PASSWORD" \
        --connect-timeout=10 \
        -e "SELECT 1" "$DB_NAME" >/dev/null 2>&1; then
    fail 2 "❌ Backup MariaDB: falha de conexão" \
        "Não foi possível conectar ao banco em ${DB_HOST}:${DB_PORT}."
fi

# Throttle opcional via pv
if [[ "${THROTTLE_MBPS}" -gt 0 ]]; then
    THROTTLE_CMD=(pv -q -L "${THROTTLE_MBPS}m")
else
    THROTTLE_CMD=(cat)
fi

# Flags de dump pensadas para NÃO travar o banco:
#  --single-transaction : snapshot consistente em InnoDB sem LOCK TABLES
#  --quick              : streaming linha-a-linha, não carrega tabela em RAM
#  --skip-lock-tables   : evita lock global
#  --skip-add-locks     : não emite LOCK TABLES no dump
#  --no-tablespaces     : não exige privilégio PROCESS
#  --routines/triggers/events : salva stored procedures, triggers e events
#  nice -n 19           : prioridade de CPU mínima
#  ionice -c 3          : classe "idle" de I/O (só roda quando disco ocioso)
set -o pipefail
if nice -n 19 ionice -c 3 \
    mariadb-dump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --user="$DB_USER" \
        --password="$DB_PASSWORD" \
        --single-transaction \
        --quick \
        --skip-lock-tables \
        --skip-add-locks \
        --no-tablespaces \
        --routines \
        --triggers \
        --events \
        --default-character-set=utf8mb4 \
        --hex-blob \
        --databases "$DB_NAME" \
  | "${THROTTLE_CMD[@]}" \
  | nice -n 19 gzip -"${GZIP_LEVEL}" \
  > "$OUTFILE"; then
    SIZE=$(du -h "$OUTFILE" | cut -f1)
    ELAPSED=$(( $(date +%s) - START ))
    log "Dump OK — tamanho: ${SIZE} — tempo: ${ELAPSED}s"
else
    rm -f "$OUTFILE"
    fail 3 "❌ Backup MariaDB: erro no dump" \
        "O mariadb-dump falhou. Arquivo incompleto removido."
fi

# Sanity check: arquivo gzip válido?
if ! gzip -t "$OUTFILE" 2>/dev/null; then
    rm -f "$OUTFILE"
    fail 4 "❌ Backup MariaDB: gzip corrompido" \
        "O arquivo gerado não passou no teste de integridade gzip e foi removido."
fi

# Upload para o Drive
log "Enviando para ${RCLONE_REMOTE}"
if rclone copy "$OUTFILE" "$RCLONE_REMOTE" \
        --transfers=1 \
        --checkers=2 \
        --retries=3 \
        --low-level-retries=10 \
        --log-file="$LOGFILE" \
        --log-level=INFO \
        --stats=0; then
    log "Upload OK"
else
    # Upload falhou. Testa a conexão para distinguir credencial vencida de
    # problema transitório de rede.
    HINT="${DASHBOARD_PUBLIC_URL:-configure DASHBOARD_PUBLIC_URL}"
    if ! rclone lsd "${RCLONE_REMOTE%%:*}:" --retries 1 --low-level-retries 1 \
            --timeout 20s >/dev/null 2>&1; then
        fail 5 "🔑 Backup: CREDENCIAL DO GOOGLE DRIVE VENCIDA" \
"Falha no upload e o rclone não consegue autenticar no Drive.
Reconecte o Google Drive na dashboard: ${HINT}
O backup local foi preservado em ${OUTFILE} e será enviado quando reconectar."
    else
        fail 5 "❌ Backup: falha no upload ao Google Drive" \
"O upload falhou (possível problema de rede/cota), mas a autenticação parece OK.
Backup local preservado em ${OUTFILE}."
    fi
fi

# Rotação local: mantém só as últimas N
log "Rotação local (manter ${LOCAL_RETENTION})"
ls -1t "${BACKUP_DIR}"/${DB_NAME}-*.sql.gz 2>/dev/null \
  | tail -n +$((LOCAL_RETENTION + 1)) \
  | while read -r old; do
      rm -f "$old" && log "  removido local: $(basename "$old")"
    done

# Rotação remota: mantém só as últimas N no Drive
log "Rotação remota (manter ${REMOTE_RETENTION})"
TOTAL_REMOTE=$(rclone lsf "$RCLONE_REMOTE" --include "${DB_NAME}-*.sql.gz" --files-only 2>/dev/null | wc -l || echo 0)
if [[ "$TOTAL_REMOTE" -gt "$REMOTE_RETENTION" ]]; then
    REMOVE_COUNT=$((TOTAL_REMOTE - REMOTE_RETENTION))
    rclone lsf "$RCLONE_REMOTE" --include "${DB_NAME}-*.sql.gz" --files-only \
      | sort \
      | head -n "$REMOVE_COUNT" \
      | while read -r f; do
          [[ -n "$f" ]] && rclone deletefile "${RCLONE_REMOTE}/${f}" \
              && log "  removido remoto: ${f}"
        done
fi

TOTAL_ELAPSED=$(( $(date +%s) - START ))
log "=== Backup concluído em ${TOTAL_ELAPSED}s ==="

# Marca o sucesso (usado pela dashboard e pelo watchdog).
date +%s > "${BACKUP_DIR}/.last_success"

# Notificação de sucesso (opcional — DISCORD_NOTIFY_SUCCESS=true).
if [[ "${DISCORD_NOTIFY_SUCCESS:-false}" == "true" ]]; then
    notify_discord ok "✅ Backup concluído: ${DB_NAME}" \
"Arquivo: ${BASENAME} (${SIZE})
Enviado para ${RCLONE_REMOTE} em ${TOTAL_ELAPSED}s."
fi
