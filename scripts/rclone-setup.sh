#!/usr/bin/env bash
# Atalho interativo para configurar o rclone dentro do container.
# Uso: docker compose exec mariadb-backup /app/scripts/rclone-setup.sh
exec rclone config
