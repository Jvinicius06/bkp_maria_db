#!/usr/bin/env python3
"""
Dashboard web do mariadb-backup.

Funções:
  - Status do último backup, lista de cópias locais e no Google Drive.
  - Teste ao vivo da conexão com o Drive (detecta credencial vencida).
  - Re-login do Google Drive via OAuth direto no navegador
    (a própria dashboard é o redirect_uri registrado no Google Cloud).
  - Disparo de backup manual.
  - Configuração/teste do webhook do Discord.

Usa apenas a biblioteca padrão do Python (sem dependências extras).
"""
import os
import io
import json
import time
import hmac
import base64
import secrets
import subprocess
import configparser
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------------------
# Configuração (via env, herdada do container)
# ----------------------------------------------------------------------------
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
BIND = os.environ.get("DASHBOARD_BIND", "0.0.0.0")
USER = os.environ.get("DASHBOARD_USER", "admin")
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
PUBLIC_URL = os.environ.get("DASHBOARD_PUBLIC_URL", "").rstrip("/")

BACKUP_DIR = os.environ.get("BACKUP_DIR", "/backups")
SETTINGS_FILE = os.environ.get("SETTINGS_FILE", os.path.join(BACKUP_DIR, "dashboard.env"))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "gdrive:backups/mariadb")
REMOTE_NAME = RCLONE_REMOTE.split(":")[0]
RCLONE_CONF = os.environ.get(
    "RCLONE_CONFIG", "/root/.config/rclone/rclone.conf"
)
DB_NAME = os.environ.get("DB_NAME", "")
DB_HOST = os.environ.get("DB_HOST", "")
CRON_SCHEDULE = os.environ.get("CRON_SCHEDULE", "0 * * * *")
LOCAL_RETENTION = os.environ.get("LOCAL_RETENTION", "3")
REMOTE_RETENTION = os.environ.get("REMOTE_RETENTION", "72")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Mapa nome-de-scope do rclone -> URL de scope OAuth do Google
SCOPE_MAP = {
    "drive": "https://www.googleapis.com/auth/drive",
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "drive.appfolder": "https://www.googleapis.com/auth/drive.appfolder",
    "drive.metadata": "https://www.googleapis.com/auth/drive.metadata.readonly",
}

_oauth_state = {}  # state -> timestamp


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def run(cmd, timeout=60, env=None):
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:  # noqa: BLE001
        return 1, "", str(e)


def human_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(ts):
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except Exception:
        return "?"


def tail(path, n=200):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            data = b""
            block = 2048
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
            return b"\n".join(data.split(b"\n")[-n:]).decode("utf-8", "replace")
    except Exception:
        return ""


def rclone_conf():
    cp = configparser.ConfigParser()
    try:
        cp.read(RCLONE_CONF)
    except Exception:
        pass
    return cp


def remote_section():
    cp = rclone_conf()
    if cp.has_section(REMOTE_NAME):
        return dict(cp.items(REMOTE_NAME))
    return {}


def client_creds():
    """client_id/secret/scope: rclone.conf tem prioridade, env como fallback."""
    sec = remote_section()
    cid = sec.get("client_id") or GOOGLE_CLIENT_ID
    csecret = sec.get("client_secret") or GOOGLE_CLIENT_SECRET
    scope = sec.get("scope") or "drive"
    return cid, csecret, scope


def token_info():
    sec = remote_section()
    raw = sec.get("token")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return {
            "expiry": data.get("expiry", ""),
            "has_refresh": bool(data.get("refresh_token")),
        }
    except Exception:
        return None


def drive_test():
    """Testa de verdade a conexão. Retorna (ok, detalhe)."""
    rc, out, err = run(
        [
            "rclone", "lsd", f"{REMOTE_NAME}:",
            "--max-depth", "1",
            "--retries", "1", "--low-level-retries", "1",
            "--timeout", "20s", "--contimeout", "15s",
        ],
        timeout=40,
    )
    return rc == 0, (err.strip() or out.strip())


def list_local():
    items = []
    try:
        for f in os.listdir(BACKUP_DIR):
            if f.endswith(".sql.gz"):
                p = os.path.join(BACKUP_DIR, f)
                st = os.stat(p)
                items.append((f, st.st_size, st.st_mtime))
    except Exception:
        pass
    items.sort(key=lambda x: x[2], reverse=True)
    return items


def list_remote():
    rc, out, err = run(
        ["rclone", "lsjson", RCLONE_REMOTE, "--files-only", "--timeout", "30s"],
        timeout=50,
    )
    if rc != 0:
        return None, err.strip() or "erro ao listar remoto"
    try:
        arr = json.loads(out or "[]")
        files = [
            (x["Name"], x.get("Size", 0), x.get("ModTime", ""))
            for x in arr
            if x.get("Name", "").endswith(".sql.gz")
        ]
        files.sort(key=lambda x: x[2], reverse=True)
        return files, None
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def last_success_age():
    marker = os.path.join(BACKUP_DIR, ".last_success")
    try:
        with open(marker) as f:
            ts = int(f.read().strip())
        return ts, (time.time() - ts)
    except Exception:
        return None, None


def discord_webhook():
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        return os.environ["DISCORD_WEBHOOK_URL"]
    try:
        with open(SETTINGS_FILE) as f:
            for line in f:
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def discord_from_env():
    return bool(os.environ.get("DISCORD_WEBHOOK_URL"))


def save_setting(key, val):
    lines = []
    try:
        with open(SETTINGS_FILE) as f:
            lines = [
                l.rstrip("\n")
                for l in f
                if not l.startswith(key + "=")
            ]
    except Exception:
        pass
    lines.append(f"{key}={val}")
    with open(SETTINGS_FILE, "w") as f:
        f.write("\n".join(lines) + "\n")


def send_discord(url, level, title, message):
    color = {"error": 15158332, "warn": 16098851, "ok": 3066993}.get(level, 9807270)
    host = os.environ.get("HOSTNAME", "container")
    payload = {
        "embeds": [{
            "title": title[:240],
            "description": message[:3900],
            "color": color,
            "footer": {"text": f"mariadb-backup @ {host}"},
        }]
    }
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------
def esc(s):
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:#0f1419;color:#e6e6e6}
header{background:#161b22;padding:16px 24px;border-bottom:1px solid #30363d;display:flex;
  align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
header h1{font-size:18px;margin:0}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-bottom:24px}
.card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:16px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.5px;color:#8b949e;margin:0 0 12px}
.big{font-size:22px;font-weight:600}
.muted{color:#8b949e}
.ok{color:#3fb950}.warn{color:#d29922}.err{color:#f85149}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:#10311c;color:#3fb950}.pill.err{background:#3d1417;color:#f85149}
.pill.warn{background:#3a2c0a;color:#d29922}
button,.btn{background:#238636;color:#fff;border:0;border-radius:6px;padding:8px 14px;
  font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
button.secondary,.btn.secondary{background:#21262d;border:1px solid #30363d;color:#e6e6e6}
button:hover{filter:brightness(1.1)}
input[type=text],input[type=url]{width:100%;padding:8px;border-radius:6px;border:1px solid #30363d;
  background:#0d1117;color:#e6e6e6;font:inherit}
pre{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;overflow:auto;
  max-height:420px;font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap}
form.inline{display:inline}
.flash{padding:12px 16px;border-radius:8px;margin-bottom:16px}
.flash.ok{background:#10311c;border:1px solid #238636}
.flash.err{background:#3d1417;border:1px solid #f85149}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
a{color:#58a6ff}
"""


def page(body, flash=None):
    f = ""
    if flash:
        kind, msg = flash
        f = f'<div class="flash {kind}">{esc(msg)}</div>'
    return f"""<!doctype html><html lang="pt-br"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MariaDB Backup</title><style>{CSS}</style></head><body>
<header><h1>🗄️ MariaDB Backup — Dashboard</h1>
<span class="muted">DB: <b>{esc(DB_NAME)}</b> @ {esc(DB_HOST)}</span></header>
<div class="wrap">{f}{body}</div></body></html>"""


def render_home(flash=None):
    # --- Status Google Drive ---
    ok, detail = drive_test()
    ti = token_info()
    if ok:
        drive_pill = '<span class="pill ok">CONECTADO</span>'
    else:
        drive_pill = '<span class="pill err">DESCONECTADO</span>'
    refresh_txt = ""
    if ti:
        refresh_txt = (
            '<div class="muted">refresh token: '
            + ('sim' if ti["has_refresh"] else '<span class="err">NÃO</span>')
            + (f' · access expira: {esc(ti["expiry"])}' if ti["expiry"] else "")
            + "</div>"
        )
    detail_html = (
        f'<div class="muted" style="margin-top:6px;font-size:12px">{esc(detail[:300])}</div>'
        if (not ok and detail) else ""
    )
    cid, _, scope = client_creds()
    reauth_block = ""
    if not PUBLIC_URL:
        reauth_block = (
            '<div class="warn" style="margin-top:8px">Defina <b>DASHBOARD_PUBLIC_URL</b> '
            "no .env para habilitar o re-login.</div>"
        )
    elif not cid:
        reauth_block = (
            '<div class="warn" style="margin-top:8px">Sem <b>client_id</b> OAuth. '
            "Configure um Client ID próprio (veja README) em GOOGLE_CLIENT_ID.</div>"
        )
    else:
        reauth_block = (
            '<div class="row"><a class="btn" href="/oauth/start">🔑 Reconectar Google Drive</a>'
            f'<span class="muted">redirect: {esc(PUBLIC_URL)}/oauth/callback · scope: {esc(scope)}</span></div>'
        )

    # --- Último backup bem-sucedido ---
    ts, age = last_success_age()
    if ts is None:
        last_pill = '<span class="pill warn">SEM REGISTRO</span>'
        last_sub = '<div class="muted">Nenhum backup concluído ainda.</div>'
    else:
        hours = age / 3600
        cls = "ok" if hours < 2 else ("warn" if hours < 6 else "err")
        last_pill = f'<span class="pill {cls}">{human_time(ts)}</span>'
        last_sub = f'<div class="muted">há {hours:.1f}h · agendamento: {esc(CRON_SCHEDULE)}</div>'

    # --- Backups locais ---
    local = list_local()
    local_rows = "".join(
        f"<tr><td>{esc(n)}</td><td>{human_size(s)}</td><td>{human_time(m)}</td></tr>"
        for n, s, m in local[:50]
    ) or '<tr><td colspan="3" class="muted">nenhum</td></tr>'

    # --- Backups no Drive ---
    remote, rerr = list_remote()
    if remote is None:
        remote_rows = f'<tr><td colspan="3" class="err">{esc(rerr or "erro")}</td></tr>'
        remote_count = "?"
    else:
        remote_count = str(len(remote))
        remote_rows = "".join(
            f"<tr><td>{esc(n)}</td><td>{human_size(s)}</td><td>{esc(m[:19].replace('T',' '))}</td></tr>"
            for n, s, m in remote[:50]
        ) or '<tr><td colspan="3" class="muted">nenhum</td></tr>'

    # --- Discord ---
    wh = discord_webhook()
    if wh:
        dc_pill = '<span class="pill ok">CONFIGURADO</span>'
        if discord_from_env():
            dc_form = '<div class="muted">Definido via .env (DISCORD_WEBHOOK_URL).</div>'
        else:
            dc_form = _discord_form(wh)
    else:
        dc_pill = '<span class="pill warn">NÃO CONFIGURADO</span>'
        dc_form = _discord_form("")
    dc_test = (
        '<form class="inline" method="post" action="/discord/test">'
        '<button class="secondary" type="submit">Enviar teste</button></form>'
        if wh else ""
    )

    return page(f"""
<div class="grid">
  <div class="card"><h2>Google Drive</h2>
    <div class="big">{drive_pill}</div>{refresh_txt}{detail_html}{reauth_block}
  </div>
  <div class="card"><h2>Último backup OK</h2>
    <div class="big">{last_pill}</div>{last_sub}
    <div class="row"><form class="inline" method="post" action="/backup/run">
      <button type="submit">▶ Rodar backup agora</button></form></div>
  </div>
  <div class="card"><h2>Cópias no Drive</h2>
    <div class="big">{remote_count} <span class="muted" style="font-size:14px">/ reter {esc(REMOTE_RETENTION)}</span></div>
    <div class="muted">local: {len(local)} / reter {esc(LOCAL_RETENTION)}</div>
  </div>
  <div class="card"><h2>Discord</h2>
    <div class="big">{dc_pill}</div>
    <div class="row">{dc_test}</div>{dc_form}
  </div>
</div>

<div class="card" style="margin-bottom:16px"><h2>Backups no Google Drive ({remote_count})</h2>
  <table><tr><th>Arquivo</th><th>Tamanho</th><th>Modificado</th></tr>{remote_rows}</table>
</div>

<div class="card" style="margin-bottom:16px"><h2>Backups locais ({len(local)})</h2>
  <table><tr><th>Arquivo</th><th>Tamanho</th><th>Modificado</th></tr>{local_rows}</table>
</div>

<div class="card"><h2>Log (backup.log)</h2>
  <div class="row"><a class="btn secondary" href="/logs?f=backup">backup.log</a>
    <a class="btn secondary" href="/logs?f=cron">cron.log</a></div>
  <pre>{esc(tail(os.path.join(BACKUP_DIR, "backup.log"), 120))}</pre>
</div>
""", flash)


def _discord_form(current):
    return (
        '<form method="post" action="/discord/save" style="margin-top:8px">'
        f'<input type="url" name="url" placeholder="https://discord.com/api/webhooks/..." '
        f'value="{esc(current)}">'
        '<div class="row"><button type="submit">Salvar webhook</button></div></form>'
    )


def render_logs(which):
    fname = "cron.log" if which == "cron" else "backup.log"
    content = tail(os.path.join(BACKUP_DIR, fname), 500)
    return page(f"""
<div class="card"><h2>{esc(fname)}</h2>
  <div class="row"><a class="btn secondary" href="/">← voltar</a>
    <a class="btn secondary" href="/logs?f=backup">backup.log</a>
    <a class="btn secondary" href="/logs?f=cron">cron.log</a></div>
  <pre>{esc(content)}</pre>
</div>""")


# ----------------------------------------------------------------------------
# OAuth
# ----------------------------------------------------------------------------
def oauth_start_url():
    cid, _, scope = client_creds()
    if not cid or not PUBLIC_URL:
        return None
    state = secrets.token_urlsafe(24)
    _oauth_state[state] = time.time()
    # limpa estados velhos (>10min)
    for k, v in list(_oauth_state.items()):
        if time.time() - v > 600:
            _oauth_state.pop(k, None)
    params = {
        "client_id": cid,
        "redirect_uri": f"{PUBLIC_URL}/oauth/callback",
        "response_type": "code",
        "scope": SCOPE_MAP.get(scope, SCOPE_MAP["drive"]),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return AUTH_URI + "?" + urllib.parse.urlencode(params)


def oauth_exchange(code):
    cid, csecret, scope = client_creds()
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cid,
        "client_secret": csecret,
        "redirect_uri": f"{PUBLIC_URL}/oauth/callback",
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(
        TOKEN_URI, data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, e.read().decode()[:500]
    except Exception as e:  # noqa: BLE001
        return None, str(e)

    if "refresh_token" not in resp:
        return None, (
            "Google não retornou refresh_token. Remova o acesso anterior em "
            "myaccount.google.com/permissions e tente de novo."
        )
    expires_in = int(resp.get("expires_in", 3600))
    expiry = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in - 10)
    ).isoformat().replace("+00:00", "Z")
    token = {
        "access_token": resp["access_token"],
        "token_type": resp.get("token_type", "Bearer"),
        "refresh_token": resp["refresh_token"],
        "expiry": expiry,
    }
    token_json = json.dumps(token)

    # Garante que o remote existe com client_id/secret/scope e grava o token.
    sec = remote_section()
    if not sec:
        rc, out, err = run([
            "rclone", "config", "create", REMOTE_NAME, "drive",
            "client_id", cid, "client_secret", csecret, "scope", scope,
            "token", token_json, "--non-interactive",
        ])
    else:
        # Atualiza client_id/secret/scope (caso viessem do env) e o token.
        run([
            "rclone", "config", "update", REMOTE_NAME,
            "client_id", cid, "client_secret", csecret, "scope", scope,
            "--non-interactive",
        ])
        rc, out, err = run([
            "rclone", "config", "update", REMOTE_NAME,
            "token", token_json, "--non-interactive",
        ])
    if rc != 0:
        return None, (err or out)[:500]
    return True, None


# ----------------------------------------------------------------------------
# HTTP handler
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "mariadb-backup-dash"

    def log_message(self, *a):  # silencia log padrão
        pass

    # -- auth --
    def authed(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(h[6:]).decode()
            u, _, p = raw.partition(":")
        except Exception:
            return False
        return hmac.compare_digest(u, USER) and hmac.compare_digest(p, PASSWORD)

    def require_auth(self):
        if self.authed():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="mariadb-backup"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Autenticação necessária".encode())
        return False

    # -- helpers de resposta --
    def html(self, body, code=200):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, code=303):
        self.send_response(code)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else ""
        return urllib.parse.parse_qs(body)

    # -- GET --
    def do_GET(self):
        if not self.require_auth():
            return
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        path = u.path

        if path == "/":
            flash = None
            if "msg" in qs:
                kind = qs.get("k", ["ok"])[0]
                flash = (kind, qs["msg"][0])
            self.html(render_home(flash))
        elif path == "/logs":
            self.html(render_logs(qs.get("f", ["backup"])[0]))
        elif path == "/oauth/start":
            url = oauth_start_url()
            if not url:
                self.redirect("/?k=err&msg=" + urllib.parse.quote(
                    "Configure DASHBOARD_PUBLIC_URL e GOOGLE_CLIENT_ID"))
            else:
                self.redirect(url, 302)
        elif path == "/oauth/callback":
            self.handle_callback(qs)
        else:
            self.html(page('<div class="card">404</div>'), 404)

    # -- POST --
    def do_POST(self):
        if not self.require_auth():
            return
        path = urllib.parse.urlparse(self.path).path

        if path == "/backup/run":
            try:
                logf = open(os.path.join(BACKUP_DIR, "cron.log"), "a")
                subprocess.Popen(
                    ["/app/scripts/backup.sh"],
                    stdout=logf, stderr=subprocess.STDOUT,
                )
                self.redirect("/?k=ok&msg=" + urllib.parse.quote(
                    "Backup disparado — acompanhe no log."))
            except Exception as e:  # noqa: BLE001
                self.redirect("/?k=err&msg=" + urllib.parse.quote(str(e)))
        elif path == "/discord/save":
            form = self.read_form()
            url = form.get("url", [""])[0].strip()
            if discord_from_env():
                self.redirect("/?k=err&msg=" + urllib.parse.quote(
                    "Webhook está fixado via .env; edite lá."))
                return
            save_setting("DISCORD_WEBHOOK_URL", url)
            self.redirect("/?k=ok&msg=" + urllib.parse.quote("Webhook salvo."))
        elif path == "/discord/test":
            wh = discord_webhook()
            try:
                send_discord(wh, "ok", "✅ Teste de webhook",
                             "Notificações do mariadb-backup estão funcionando.")
                self.redirect("/?k=ok&msg=" + urllib.parse.quote("Teste enviado."))
            except Exception as e:  # noqa: BLE001
                self.redirect("/?k=err&msg=" + urllib.parse.quote(
                    "Falha: " + str(e)))
        else:
            self.html(page('<div class="card">404</div>'), 404)

    def handle_callback(self, qs):
        if "error" in qs:
            self.redirect("/?k=err&msg=" + urllib.parse.quote(
                "Google retornou erro: " + qs["error"][0]))
            return
        state = qs.get("state", [""])[0]
        if state not in _oauth_state:
            self.redirect("/?k=err&msg=" + urllib.parse.quote(
                "State inválido/expirado. Tente reconectar de novo."))
            return
        _oauth_state.pop(state, None)
        code = qs.get("code", [""])[0]
        if not code:
            self.redirect("/?k=err&msg=" + urllib.parse.quote("Sem code."))
            return
        ok, err = oauth_exchange(code)
        if ok:
            self.redirect("/?k=ok&msg=" + urllib.parse.quote(
                "Google Drive reconectado com sucesso!"))
        else:
            self.redirect("/?k=err&msg=" + urllib.parse.quote(
                "Falha ao trocar token: " + (err or "")))


def main():
    if not PASSWORD:
        print("[dashboard] ERRO: DASHBOARD_PASSWORD vazio — dashboard não iniciada.")
        print("[dashboard] Defina DASHBOARD_PASSWORD no .env para habilitar.")
        return
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"[dashboard] ouvindo em http://{BIND}:{PORT} (user={USER})")
    if PUBLIC_URL:
        print(f"[dashboard] redirect OAuth: {PUBLIC_URL}/oauth/callback")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
