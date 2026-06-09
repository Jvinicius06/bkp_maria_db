#!/usr/bin/env bash
# Helper de notificação Discord — sourceável por backup.sh e watchdog.sh.
#
# Resolve o webhook nesta ordem de prioridade:
#   1. Variável de ambiente DISCORD_WEBHOOK_URL (definida no .env)
#   2. Arquivo de settings gravado pela dashboard (DISCORD_WEBHOOK_URL=...)

SETTINGS_FILE="${SETTINGS_FILE:-/backups/dashboard.env}"

_discord_url() {
    if [[ -n "${DISCORD_WEBHOOK_URL:-}" ]]; then
        printf '%s' "${DISCORD_WEBHOOK_URL}"
        return
    fi
    if [[ -f "${SETTINGS_FILE}" ]]; then
        grep -E '^DISCORD_WEBHOOK_URL=' "${SETTINGS_FILE}" 2>/dev/null \
            | tail -n1 | cut -d= -f2-
    fi
}

# Monta o payload JSON com escape seguro usando python3 (já presente na imagem).
_discord_payload() {
    # args: color title message host
    python3 - "$@" <<'PY'
import sys, json
color, title, message, host = sys.argv[1:5]
print(json.dumps({
    "embeds": [{
        "title": title[:240],
        "description": message[:3900],
        "color": int(color),
        "footer": {"text": f"mariadb-backup @ {host}"},
    }]
}))
PY
}

# notify_discord <nivel> <titulo> <mensagem>
#   nivel: error | warn | ok
notify_discord() {
    local level="$1" title="$2" message="$3"
    local url; url="$(_discord_url)"
    [[ -z "${url}" ]] && return 0

    local color
    case "${level}" in
        error) color=15158332 ;;  # vermelho
        warn)  color=16098851 ;;  # laranja
        ok)    color=3066993  ;;  # verde
        *)     color=9807270  ;;  # cinza
    esac

    local host="${HOSTNAME:-container}"
    local payload; payload="$(_discord_payload "${color}" "${title}" "${message}" "${host}")"

    curl -fsS -m 15 -H "Content-Type: application/json" \
        -X POST -d "${payload}" "${url}" >/dev/null 2>&1 || true
}
