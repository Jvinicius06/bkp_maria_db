# Backup MariaDB → Google Drive

Sistema de backup automático do MariaDB com **baixo impacto no banco em produção**, upload para o Google Drive e rotação automática de arquivos antigos.

Pensado para bancos que ficam sob carga e não podem travar durante o dump.

---

## Como funciona

Um container Docker executa `mariadb-dump` a cada hora (configurável) usando flags que evitam locks, com prioridade mínima de CPU/disco e throughput limitado. O arquivo `.sql.gz` é enviado ao Google Drive via `rclone` e cópias antigas são removidas automaticamente.

### Por que é gentil com o banco

| Técnica | Efeito |
|---|---|
| `--single-transaction` | Snapshot consistente em InnoDB **sem `LOCK TABLES`** |
| `--quick` | Streaming linha-a-linha (não carrega tabela em RAM) |
| `--skip-lock-tables --skip-add-locks` | Zero travamento de tabelas |
| `nice -n 19` | Prioridade de CPU mínima |
| `ionice -c 3` | Classe "idle" de I/O — só usa disco quando ocioso |
| `pv -L ${THROTTLE_MBPS}m` | **Limita vazão do pipe** (padrão 5 MB/s) |
| `gzip -1` | Compressão rápida, baixo custo de CPU |
| `mem_limit: 512m`, `cpus: 0.5` | Limite duro do container |

O ajuste mais importante é **`THROTTLE_MBPS`**: quanto menor, menos stress no banco (e mais demorado o backup).

---

## Estrutura

```
.
├── Dockerfile
├── docker-compose.yml
├── .env.example           # copie para .env e edite
├── crontab
└── scripts/
    ├── backup.sh          # dump + upload + rotação
    ├── entrypoint.sh      # prepara env e roda o cron
    ├── restore.sh         # restauração (arquivo local ou do Drive)
    └── rclone-setup.sh    # atalho para configurar o Google Drive
```

---

## Pré-requisitos

- Docker + Docker Compose
- Acesso ao MariaDB (host alcançável desde o container)
- Conta Google com Drive habilitado

---

## Instalação passo a passo

### 1. Criar usuário de backup no MariaDB

Com privilégios mínimos:

```sql
CREATE USER 'backup_user'@'%' IDENTIFIED BY 'troque_esta_senha';
GRANT SELECT, SHOW VIEW, EVENT, TRIGGER, LOCK TABLES, RELOAD, REPLICATION CLIENT
  ON *.* TO 'backup_user'@'%';
FLUSH PRIVILEGES;
```

> Se o banco aceita conexão só do localhost, ajuste o host (`'%'`) para o IP/rede do Docker, ou use `host.docker.internal`.

### 2. Configurar variáveis de ambiente

```bash
cp .env.example .env
```

Edite o `.env`:

| Variável | Descrição |
|---|---|
| `DB_HOST` | `host.docker.internal` (banco no host) ou nome do serviço Docker |
| `DB_PORT` | Padrão `3306` |
| `DB_USER` / `DB_PASSWORD` | Usuário criado no passo 1 |
| `DB_NAME` | Nome do banco a salvar |
| `RCLONE_REMOTE` | Ex: `gdrive:backups/mariadb` (pasta será criada) |
| `THROTTLE_MBPS` | Limite de vazão em MB/s (0 = sem limite) |
| `GZIP_LEVEL` | `1` (rápido) a `9` (melhor compressão) |
| `LOCAL_RETENTION` | Nº de backups locais a manter |
| `REMOTE_RETENTION` | Nº de backups no Drive a manter |
| `CRON_SCHEDULE` | Agendamento cron (padrão: `0 * * * *` — toda hora cheia) |
| `RUN_ON_START` | `true` faz um backup imediato ao subir o container |

> **Evite aspas simples (`'`) dentro da senha** — o entrypoint exporta as variáveis usando aspas simples.

### 3. Buildar a imagem

```bash
docker compose build
```

### 4. Configurar o rclone para o Google Drive (uma vez)

```bash
docker compose run --rm mariadb-backup rclone config
```

Passos interativos:

1. `n` → **New remote**
2. Nome: **`gdrive`** (tem que casar com o prefixo de `RCLONE_REMOTE`)
3. Storage: **`drive`** (Google Drive)
4. `client_id` / `client_secret`: deixe vazio para usar o padrão, **ou** (recomendado) crie um próprio em [console.cloud.google.com](https://console.cloud.google.com) → APIs & Services → Credentials → OAuth client ID (Desktop app)
5. Scope: **`2`** (`drive.file` — acessa só o que o rclone criou, mais seguro) ou **`1`** (acesso total)
6. `service_account_file`: enter (vazio)
7. `Edit advanced config?`: `n`
8. `Use auto config?`: **`n`** (porque está em container)
9. Abra o link que o rclone mostrar, autorize, cole o token de volta
10. `Configure this as a Shared Drive?`: `n` (a menos que seja)
11. `y` → confirma
12. `q` → sai

A configuração fica em `./rclone-config/rclone.conf` (persistido via volume).

### 5. Testar com um backup imediato

```bash
RUN_ON_START=true docker compose up -d
docker compose logs -f
```

Você deve ver algo como:

```
[entrypoint] schedule: 0 * * * *
[entrypoint] RUN_ON_START=true — disparando backup agora
=== Iniciando backup de 'meu_banco' em host.docker.internal:3306 ===
Dump OK — tamanho: 42M — tempo: 28s
Upload OK
=== Backup concluído em 35s ===
```

Para parar o tail: `Ctrl+C` (o container continua rodando).

### 6. Deixar rodando em produção

```bash
docker compose up -d
```

---

## Uso no dia-a-dia

### Ver logs

```bash
docker compose logs -f          # logs do container
tail -f backups/backup.log      # log detalhado dos backups
```

### Disparar um backup manual

```bash
docker compose exec mariadb-backup /app/scripts/backup.sh
```

### Listar backups no Drive

```bash
docker compose exec mariadb-backup rclone ls gdrive:backups/mariadb
```

### Restaurar

Arquivo local:

```bash
docker compose exec mariadb-backup \
  /app/scripts/restore.sh /backups/meu_banco-20260423-140000.sql.gz
```

Direto do Drive:

```bash
docker compose exec mariadb-backup \
  /app/scripts/restore.sh gdrive:backups/mariadb/meu_banco-20260423-140000.sql.gz
```

Restaurar em outro banco:

```bash
docker compose exec mariadb-backup \
  /app/scripts/restore.sh /backups/meu_banco-20260423-140000.sql.gz banco_teste
```

### Parar / reiniciar

```bash
docker compose stop
docker compose restart
docker compose down            # remove container (mantém volumes)
```

---

## Ajustes se o banco ainda sentir impacto

No `.env`:

- **Reduza `THROTTLE_MBPS`** para `2` ou `1` — mais lento, muito menos carga
- **Evite horários de pico** no `CRON_SCHEDULE`:
  - Só de madrugada: `0 0-6,22-23 * * *`
  - A cada 2h: `0 */2 * * *`
  - A cada 4h: `0 */4 * * *`
- Se o dump ainda pesa no I/O, aumente o `GZIP_LEVEL` para `6` — o dump sai mais devagar do banco mas gzip consome mais CPU; teste qual trade-off é melhor no seu servidor

---

## Troubleshooting

**"Não consegui conectar em DB_HOST"**
- Se MariaDB roda no host (Windows/Linux): `DB_HOST=host.docker.internal` e o compose já define `extra_hosts`
- Se roda em outro container Docker: adicione o container à mesma rede (`networks` no compose) e use o nome do serviço como `DB_HOST`
- Verifique se o `bind-address` do MariaDB não está travado em `127.0.0.1`

**"remote 'gdrive' não configurado no rclone"**
- Refaça o passo 4. O arquivo `./rclone-config/rclone.conf` deve existir e conter uma seção `[gdrive]`

**Upload falha com erro 403 / rate limit**
- Crie seu próprio `client_id`/`client_secret` em console.cloud.google.com (evita cota compartilhada do rclone)

**Arquivo ficou muito grande para o Drive**
- Aumente `GZIP_LEVEL` para `6` ou `9`
- Considere backup só das tabelas essenciais (exclua tabelas de log/cache via `--ignore-table` — requer editar o `backup.sh`)

---

## Para bancos muito grandes (>10GB)

Se mesmo com throttle agressivo o dump lógico impactar a produção, o caminho correto é **backup físico incremental** com `mariabackup` + captura de `binlog`. É outro setup (requer acesso ao filesystem do MariaDB e mais espaço). Avise se precisar dessa arquitetura.
