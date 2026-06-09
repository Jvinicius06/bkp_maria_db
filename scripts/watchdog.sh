#!/usr/bin/env bash
# Watchdog: alerta no Discord se faz muito tempo que não há backup BEM-SUCEDIDO.
# Pega o caso clássico de "a credencial do Drive venceu e ninguém percebeu":
# o backup.sh grava /backups/.last_success só depois do upload OK.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=/dev/null
. "${SCRIPT_DIR}/notify.sh"

BACKUP_DIR="${BACKUP_DIR:-/backups}"
MARKER="${BACKUP_DIR}/.last_success"
MAX_AGE_HOURS="${BACKUP_MAX_AGE_HOURS:-3}"
DASHBOARD_HINT="${DASHBOARD_PUBLIC_URL:-N/A}"

now=$(date +%s)

if [[ ! -f "${MARKER}" ]]; then
    notify_discord error "⚠️ Watchdog: nenhum backup bem-sucedido registrado" \
"Não há registro de backup concluído para '${DB_NAME:-?}'.
Verifique o serviço e a conexão com o Google Drive.
Dashboard: ${DASHBOARD_HINT}"
    exit 0
fi

last=$(cat "${MARKER}" 2>/dev/null || echo 0)
age_h=$(( (now - last) / 3600 ))

if (( age_h >= MAX_AGE_HOURS )); then
    notify_discord error "⚠️ Watchdog: backup atrasado (${age_h}h)" \
"O último backup BEM-SUCEDIDO de '${DB_NAME:-?}' foi há ${age_h}h (limite: ${MAX_AGE_HOURS}h).
Causa provável: credencial do Google Drive vencida.
👉 Reconecte o Drive na dashboard: ${DASHBOARD_HINT}"
fi
