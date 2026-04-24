FROM alpine:3.19

RUN apk add --no-cache \
    mariadb-client \
    mariadb-connector-c \
    rclone \
    busybox-suid \
    pv \
    gzip \
    bash \
    tzdata \
    coreutils \
    util-linux \
    ca-certificates

ENV TZ=America/Sao_Paulo

WORKDIR /app

COPY scripts/ /app/scripts/
COPY crontab /etc/crontabs/root

RUN chmod +x /app/scripts/*.sh \
    && mkdir -p /backups /root/.config/rclone \
    && touch /var/log/cron.log

VOLUME ["/backups", "/root/.config/rclone"]

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
