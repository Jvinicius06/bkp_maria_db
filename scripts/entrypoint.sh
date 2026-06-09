#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] timezone: ${TZ:-UTC}"

# Cron não herda env vars do container. Exportamos tudo que importa para um
# arquivo que o crontab fará source antes de rodar o backup.
{
    echo "#!/usr/bin/env bash"
    printenv | grep -E '^(DB_|RCLONE_|BACKUP_|THROTTLE_|GZIP_|LOCAL_RETENTION|REMOTE_RETENTION|DISCORD_|DASHBOARD_|GOOGLE_|SETTINGS_FILE|TZ)=' \
        | sed 's/^\([^=]*\)=\(.*\)$/export \1='"'"'\2'"'"'/'
} > /app/env.sh
chmod +x /app/env.sh

# Substitui o placeholder do crontab pelo schedule escolhido
SCHEDULE="${CRON_SCHEDULE:-0 * * * *}"
sed "s|__CRON_PLACEHOLDER__|${SCHEDULE}|g" /etc/crontabs/root > /etc/crontabs/root.tmp
mv /etc/crontabs/root.tmp /etc/crontabs/root
chmod 0600 /etc/crontabs/root

echo "[entrypoint] schedule: ${SCHEDULE}"
echo "[entrypoint] crontab:"
cat /etc/crontabs/root

# Valida config do rclone
if ! rclone listremotes 2>/dev/null | grep -q "^${RCLONE_REMOTE%%:*}:"; then
    echo "[entrypoint] AVISO: remote '${RCLONE_REMOTE%%:*}' não configurado no rclone."
    echo "[entrypoint] Rode 'docker compose exec mariadb-backup rclone config' para configurar."
fi

# Backup imediato no startup (opcional)
if [[ "${RUN_ON_START:-false}" == "true" ]]; then
    echo "[entrypoint] RUN_ON_START=true — disparando backup agora"
    /app/scripts/backup.sh || echo "[entrypoint] backup inicial falhou (seguindo mesmo assim)"
fi

# Tail do log em paralelo para que 'docker logs' mostre execuções do cron
touch /backups/cron.log /backups/backup.log
tail -F /backups/cron.log /backups/backup.log &

# Sobe a dashboard web (se houver senha definida) com auto-restart.
if [[ -n "${DASHBOARD_PASSWORD:-}" ]]; then
    echo "[entrypoint] iniciando dashboard na porta ${DASHBOARD_PORT:-8080}"
    (
        while true; do
            python3 /app/scripts/dashboard.py || echo "[dashboard] saiu, reiniciando em 5s"
            sleep 5
        done
    ) &
else
    echo "[entrypoint] DASHBOARD_PASSWORD não definido — dashboard desabilitada."
fi

echo "[entrypoint] iniciando crond (busybox) em foreground"
# Usa o crond do busybox (-f foreground, -l 8 log level, -L arquivo de log)
exec busybox crond -f -l 8 -L /dev/stderr
