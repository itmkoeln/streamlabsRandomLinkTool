#!/usr/bin/env python3
"""LineRoulette - serves plain-text endpoints for chat bots (Python standard library only).

Public output (text/plain, no cache):
  subdomain mode (BASE_DOMAIN set):  https://<channel>.<BASE_DOMAIN>/<name>
  path mode (always available):      https://<host>/c/<channel>/<name>

Admin interface: /admin  (login form, sessions, CSRF protection, brute-force lock, multiple users)

Endpoint types:
  list      table of lines; mode: random | sequence | all
  document  free text with placeholders

Placeholders (in lines and documents):
  {date} {time} {datetime} {weekday}   current date/time (formats: DATE_FORMAT/TIME_FORMAT, names: CONTENT_LANG)
  {number:1-100}                        random number
  {pick:a|b|c}                          random choice inline
  {line:name}                           random line of another list in the same channel
  {doc:name}                            output of another endpoint in the same channel

Environment variables (all optional except ADMIN_PASSWORD on first start):
  ADMIN_USER (admin) · ADMIN_PASSWORD (min. 10 chars; creates the first user when none exists)
  ADMIN_RESET=1          reset the password of ADMIN_USER at startup (remove afterwards)
  BASE_DOMAIN            enables subdomain mode, e.g. hub.example.com -> <channel>.hub.example.com
  ADMIN_HOST             host name that always serves the admin interface, e.g. admin.example.com
  PUBLIC_SCHEME (https) · PUBLIC_URL (public base URL shown in path mode)
  TRUST_PROXY=1          behind a reverse proxy: use X-Forwarded-For/-Proto/-Host
  COOKIE_SECURE=1        always set the Secure flag on cookies
  SESSION_HOURS (12) · DEFAULT_LANG (en) · CONTENT_LANG (= DEFAULT_LANG)
  DATE_FORMAT (%Y-%m-%d) · TIME_FORMAT (%H:%M) · DB_PATH (/data/data.db) · PORT (8080) · TZ
"""
import glob
import hashlib
import hmac
import html
import json
import math
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

APP_NAME = "LineRoulette"
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH") or "/data/data.db"
PORT = int(os.environ.get("PORT") or "8080")
ADMIN_USER = os.environ.get("ADMIN_USER") or "admin"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_RESET = os.environ.get("ADMIN_RESET", "0") == "1"
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_TTL = int(float(os.environ.get("SESSION_HOURS") or "12") * 3600)
SESSION_MAX = 7 * 24 * 3600
BASE_DOMAIN = os.environ.get("BASE_DOMAIN", "").strip().lower().strip(".")
PUBLIC_SCHEME = os.environ.get("PUBLIC_SCHEME") or "https"
ADMIN_HOST = os.environ.get("ADMIN_HOST", "").strip().lower()
DATE_FORMAT = os.environ.get("DATE_FORMAT") or "%Y-%m-%d"
TIME_FORMAT = os.environ.get("TIME_FORMAT") or "%H:%M"
COOKIE_NAME = "ph_session"
LANG_COOKIE = "ph_lang"
MAX_FAILS = 5
LOCK_SECONDS = 600
MIN_PW = 10

USER_RE = re.compile(r"^[a-z0-9._-]{3,32}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CHAN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
RESERVED_CHANNELS = {"www", "admin", "api", "mail"}
MODES = ("random", "sequence", "all")

if hasattr(time, "tzset"):
    time.tzset()


# ---------------------------------------------------------------- i18n
def load_locales():
    out = {}
    for f in sorted(glob.glob(os.path.join(os.environ.get("LOCALE_DIR", os.path.join(HERE, "locales")), "*.json"))):
        code = os.path.basename(f)[:-5].lower()
        if re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", code):
            with open(f, encoding="utf-8") as fh:
                out[code] = json.load(fh)
    return out


LOCALES = load_locales()
if "en" not in LOCALES:
    sys.exit("locales/en.json is missing")
DEFAULT_LANG = (os.environ.get("DEFAULT_LANG") or "en").strip().lower()
if DEFAULT_LANG not in LOCALES:
    DEFAULT_LANG = "en"
CONTENT_LANG = (os.environ.get("CONTENT_LANG") or DEFAULT_LANG).strip().lower()
if CONTENT_LANG not in LOCALES:
    CONTENT_LANG = DEFAULT_LANG
WEEKDAYS = LOCALES[CONTENT_LANG].get("weekdays") or LOCALES["en"]["weekdays"]


def tr(lang, key, **kw):
    v = LOCALES.get(lang, {}).get(key)
    if v is None:
        v = LOCALES["en"].get(key, key)
    return v.format(**kw) if kw else v


# ---------------------------------------------------------------- passwords
def hash_pw(pw, salt=None):
    salt = salt if salt is not None else os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8")[:1024], salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex(), h.hex()


_DUMMY = hash_pw("dummy-password-for-constant-runtime")


def verify_user(con, username, password):
    """User row or None. Runtime does not depend on whether the user exists."""
    row = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    salt_hex, expected = (row["pw_salt"], row["pw_hash"]) if row else _DUMMY
    _, h = hash_pw(password, bytes.fromhex(salt_hex))
    ok = hmac.compare_digest(h, expected)
    return row if (row and ok) else None


def pw_error(pw, pw2):
    """Returns a locale key suffix (err.<x>) or ''."""
    if pw != pw2:
        return "pw_mismatch"
    if len(pw) < MIN_PW:
        return "pw_short"
    if len(pw) > 256:
        return "pw_long"
    return ""


# ---------------------------------------------------------------- brute-force lock
_fail_lock = threading.Lock()
_fails = {}


def lock_remaining(key):
    with _fail_lock:
        rec = _fails.get(key)
        if rec and rec["until"] > time.time():
            return int(rec["until"] - time.time()) + 1
    return 0


def register_fail(key, limit):
    now = time.time()
    with _fail_lock:
        rec = _fails.get(key)
        if not rec or (rec["until"] < now and now - rec["first"] > LOCK_SECONDS):
            rec = {"n": 0, "first": now, "until": 0}
        rec["n"] += 1
        if rec["n"] >= limit:
            rec.update(n=0, first=now, until=now + LOCK_SECONDS)
        _fails[key] = rec
        if len(_fails) > 5000:
            for k in [k for k, v in _fails.items() if v["until"] < now and now - v["first"] > LOCK_SECONDS]:
                _fails.pop(k, None)


def clear_fails(*keys):
    with _fail_lock:
        for k in keys:
            _fails.pop(k, None)


# ---------------------------------------------------------------- database
def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


CHANNELS_SQL = """
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    created INTEGER NOT NULL
);
"""

ENDPOINTS_SQL = """
CREATE TABLE {name}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'list',
    mode TEXT NOT NULL DEFAULT 'random',
    body TEXT NOT NULL DEFAULT '',
    counter INTEGER NOT NULL DEFAULT 0,
    UNIQUE(channel_id, slug)
);
"""


def migrate_to_channels(con):
    """Old database without channels: all endpoints move into the channel 'default'."""
    cols = [r["name"] for r in con.execute("PRAGMA table_info(endpoints)")]
    if not cols or "channel_id" in cols:
        return
    con.commit()
    con.execute("PRAGMA foreign_keys=OFF")
    con.executescript(CHANNELS_SQL)
    con.execute("INSERT OR IGNORE INTO channels(id,name,title,created) VALUES(1,'default','Default',?)", (int(time.time()),))
    con.executescript(ENDPOINTS_SQL.format(name="endpoints_new"))
    con.execute(
        "INSERT INTO endpoints_new(id,channel_id,slug,title,kind,mode,body,counter) "
        "SELECT id,1,slug,title,kind,mode,body,counter FROM endpoints"
    )
    con.executescript("DROP TABLE endpoints; ALTER TABLE endpoints_new RENAME TO endpoints;")
    con.execute("PRAGMA foreign_keys=ON")
    con.commit()
    print("Database migrated to channels (existing endpoints are in channel 'default').", flush=True)


def init_db():
    con = db()
    cols = [r["name"] for r in con.execute("PRAGMA table_info(sessions)")]
    if cols and "user_id" not in cols:
        con.execute("DROP TABLE sessions")
    con.executescript(CHANNELS_SQL)
    migrate_to_channels(con)
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='endpoints'").fetchone():
        con.executescript(ENDPOINTS_SQL.format(name="endpoints"))
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pw_salt TEXT NOT NULL,
            pw_hash TEXT NOT NULL,
            created INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf TEXT NOT NULL,
            expires INTEGER NOT NULL,
            created INTEGER NOT NULL
        );
        """
    )
    con.commit()
    bootstrap_admin(con)
    con.close()


def bootstrap_admin(con):
    """Creates the first user from ADMIN_USER/ADMIN_PASSWORD (only if none exists) or resets it."""
    name = ADMIN_USER.strip().lower()
    count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not ADMIN_PASSWORD:
        if count == 0:
            print("WARNING: no user exists and ADMIN_PASSWORD is not set - nobody can sign in.", flush=True)
        return
    if len(ADMIN_PASSWORD) < MIN_PW or not USER_RE.match(name):
        print(f"WARNING: invalid ADMIN_USER (3-32 chars: a-z 0-9 . _ -) or ADMIN_PASSWORD (min. {MIN_PW} chars).", flush=True)
        return
    if count == 0 or ADMIN_RESET:
        salt, h = hash_pw(ADMIN_PASSWORD)
        con.execute(
            "INSERT INTO users(username,pw_salt,pw_hash,created) VALUES(?,?,?,?) "
            "ON CONFLICT(username) DO UPDATE SET pw_salt=excluded.pw_salt, pw_hash=excluded.pw_hash",
            (name, salt, h, int(time.time())),
        )
        con.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (name,))
        con.commit()
        print(f"User '{name}' {'reset' if ADMIN_RESET and count else 'created'}.", flush=True)


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def new_session(con, user_id):
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    now = int(time.time())
    con.execute("DELETE FROM sessions WHERE expires<?", (now,))
    con.execute(
        "INSERT INTO sessions(id,user_id,csrf,expires,created) VALUES(?,?,?,?,?)",
        (_sha(token), user_id, csrf, now + SESSION_TTL, now),
    )
    con.commit()
    return token


# ---------------------------------------------------------------- rendering
def render_endpoint(con, channel_id, slug, depth=0, advance=True):
    ep = con.execute("SELECT * FROM endpoints WHERE channel_id=? AND slug=?", (channel_id, slug)).fetchone()
    if ep is None:
        return None
    if ep["kind"] == "document":
        return expand(con, ep["body"], depth, advance, channel_id)
    rows = con.execute("SELECT text FROM items WHERE endpoint_id=? AND active=1 ORDER BY id", (ep["id"],)).fetchall()
    texts = [r["text"] for r in rows]
    if not texts:
        return ""
    if ep["mode"] == "all":
        out = "\n".join(texts)
    elif ep["mode"] == "sequence":
        out = texts[ep["counter"] % len(texts)]
        if advance:
            con.execute("UPDATE endpoints SET counter=counter+1 WHERE id=?", (ep["id"],))
            con.commit()
    else:
        out = random.choice(texts)
    return expand(con, out, depth, advance, channel_id)


PLACEHOLDER = re.compile(r"\{(date|time|datetime|weekday|number:\d+-\d+|pick:[^{}]*|line:[a-z0-9_-]+|doc:[a-z0-9_-]+)\}")


def expand(con, text, depth, advance, channel_id):
    """{line:..} and {doc:..} only resolve endpoints of the same channel."""
    if depth >= 3:
        return text

    def sub(m):
        key = m.group(1)
        now = time.localtime()
        if key == "date":
            return time.strftime(DATE_FORMAT, now)
        if key == "time":
            return time.strftime(TIME_FORMAT, now)
        if key == "datetime":
            return time.strftime(DATE_FORMAT + " " + TIME_FORMAT, now)
        if key == "weekday":
            return WEEKDAYS[now.tm_wday]
        if key.startswith("number:"):
            a, b = (int(x) for x in key[7:].split("-"))
            return str(random.randint(min(a, b), max(a, b)))
        if key.startswith("pick:"):
            opts = [o for o in key[5:].split("|") if o != ""]
            return random.choice(opts) if opts else ""
        if key.startswith("line:") or key.startswith("doc:"):
            res = render_endpoint(con, channel_id, key.split(":", 1)[1], depth + 1, advance)
            return res if res is not None else m.group(0)
        return m.group(0)

    return PLACEHOLDER.sub(sub, text)


# ---------------------------------------------------------------- UI
CSS = """
:root{--bg:#f4f5f9;--card:#fff;--text:#1b1f2a;--muted:#6b7280;--line:#e4e6ef;--accent:#5b5bf0;--accent-h:#4646d6;
--accent-soft:#ecebff;--danger:#d92d2d;--danger-soft:#fdecec;--ok:#16a34a;--radius:14px;
--shadow:0 1px 2px rgba(16,24,40,.05),0 6px 20px rgba(16,24,40,.05)}
@media(prefers-color-scheme:dark){:root{--bg:#0e1016;--card:#171a23;--text:#e8eaf1;--muted:#9aa1b3;--line:#262a38;
--accent:#8c8cff;--accent-h:#a5a5ff;--accent-soft:#242449;--danger:#f87171;--danger-soft:#3a1c1f;--ok:#4ade80;--shadow:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:1.55rem;margin:0;letter-spacing:-.01em}h2{font-size:1.05rem;margin:0 0 .9rem}p{margin:.3rem 0}
.topbar{position:sticky;top:0;z-index:5;background:color-mix(in srgb,var(--card) 92%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line)}
.topbar .in{max-width:980px;margin:0 auto;padding:.65rem 1rem;display:flex;align-items:center;gap:.8rem}
.topbar form{margin:0}.sp{flex:1}
.brand{display:flex;align-items:center;gap:.6rem;font-weight:700;color:var(--text);font-size:1.05rem}.brand:hover{text-decoration:none}
.logo{width:32px;height:32px;border-radius:10px;background:linear-gradient(135deg,var(--accent),#22d3ee);display:grid;place-items:center}
.logo .ic{width:18px;height:18px;color:#fff}
.wrap{max-width:980px;margin:0 auto;padding:1.5rem 1rem 3rem}
.head{display:flex;align-items:center;gap:.9rem;flex-wrap:wrap;margin-bottom:1.2rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:1.1rem 1.2rem;box-shadow:var(--shadow);margin-bottom:1.1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:1rem;margin-bottom:1.1rem}
.ep{display:flex;flex-direction:column;gap:.65rem;margin:0;transition:transform .12s,border-color .12s}
.ep:hover{transform:translateY(-2px);border-color:var(--accent)}
.ep .t{font-weight:700;font-size:1.05rem;color:var(--text)}
.row{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:.4rem;border:1px solid var(--line);background:var(--card);color:var(--text);
padding:.5rem .9rem;border-radius:10px;font:inherit;font-weight:600;cursor:pointer;transition:background .12s,border-color .12s}
.btn:hover{border-color:var(--accent);text-decoration:none}.btn:active{transform:translateY(1px)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn.primary:hover{background:var(--accent-h)}
.btn.ghost{background:transparent}
.btn.danger{color:var(--danger)}.btn.danger:hover{background:var(--danger-soft);border-color:var(--danger)}
.btn.sm{padding:.3rem .65rem;font-size:.85rem}.btn.icon{padding:.42rem}
.btn.block{width:100%;justify-content:center;padding:.7rem}
.ic{width:16px;height:16px;flex:none}
input[type=text],input[type=password],textarea{width:100%;padding:.55rem .7rem;border:1px solid var(--line);border-radius:10px;
background:var(--bg);color:var(--text);font:inherit}
input:focus,textarea:focus,.btn:focus-visible,.switch:focus-visible{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent)}
textarea{min-height:11rem;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.9rem;resize:vertical}
label.f{display:block;font-weight:600;font-size:.85rem;margin-bottom:.95rem}
label.f>input,label.f>textarea,label.f>.pwrow{margin-top:.35rem;font-weight:400}
.muted{color:var(--muted)}.small{font-size:.85rem}
.badge{display:inline-block;padding:.12rem .6rem;border-radius:99px;font-size:.75rem;font-weight:600;background:var(--accent-soft);color:var(--accent)}
.badge.gray{background:var(--bg);color:var(--muted);border:1px solid var(--line)}
.urlbox{display:flex;align-items:center;gap:.5rem;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:.3rem .3rem .3rem .7rem}
.urlbox code{flex:1;overflow:auto;white-space:nowrap;font:.84rem ui-monospace,Menlo,monospace}
.lbl{font-size:.78rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:.8rem 0 .3rem}.lbl:first-child{margin-top:0}
.pre{margin:.6rem 0 0;padding:.8rem;border-radius:10px;background:var(--bg);border:1px dashed var(--line);white-space:pre-wrap;
word-break:break-word;font:.9rem ui-monospace,Menlo,monospace;min-height:2.6rem}
.item{display:flex;gap:.5rem;align-items:center;padding:.4rem 0;border-bottom:1px solid var(--line)}
.item:last-child{border-bottom:0}.item[hidden]{display:none}.item form{margin:0}.item .grow{flex:1;display:flex;gap:.4rem}
.item.off input[type=text]{opacity:.5;text-decoration:line-through}
.switch{width:42px;height:24px;border-radius:99px;border:0;background:var(--line);position:relative;cursor:pointer;padding:0;transition:background .15s}
.switch i{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:transform .15s}
.switch.on{background:var(--ok)}.switch.on i{transform:translateX(18px)}
.seg{display:flex;gap:.4rem;flex-wrap:wrap;margin:.35rem 0 .3rem}.seg label{position:relative}
.seg input{position:absolute;opacity:0;inset:0;cursor:pointer}
.seg span{display:block;padding:.45rem .9rem;border:1px solid var(--line);border-radius:10px;font-weight:600;font-size:.88rem;background:var(--bg)}
.seg input:checked+span{background:var(--accent);border-color:var(--accent);color:#fff}
.seg input:focus-visible+span{outline:2px solid var(--accent);outline-offset:2px}
.chips{display:flex;gap:.4rem;flex-wrap:wrap;margin:.2rem 0 .8rem}
.chip{border:1px solid var(--line);background:var(--bg);color:var(--text);padding:.2rem .6rem;border-radius:99px;font:.8rem ui-monospace,Menlo,monospace;cursor:pointer}
.chip:hover{border-color:var(--accent);color:var(--accent)}
.alert{padding:.65rem .9rem;border-radius:10px;margin-bottom:1rem;font-size:.9rem;background:var(--danger-soft);color:var(--danger);border:1px solid var(--danger)}
dialog{border:1px solid var(--line);border-radius:var(--radius);background:var(--card);color:var(--text);padding:1.3rem;width:min(460px,92vw);box-shadow:0 20px 60px rgba(0,0,0,.3)}
dialog::backdrop{background:rgba(10,12,20,.55);backdrop-filter:blur(2px)}
.toast{position:fixed;left:50%;bottom:1.5rem;transform:translateX(-50%);background:var(--text);color:var(--bg);padding:.6rem 1rem;border-radius:10px;
font-weight:600;box-shadow:0 8px 30px rgba(0,0,0,.25);z-index:50;transition:opacity .4s;max-width:92vw}
.toast.err{background:var(--danger);color:#fff}.toast.out{opacity:0}
.auth{min-height:100vh;display:grid;place-items:center;padding:1rem}
.authbox{width:min(400px,100%)}
.login{width:100%;margin:0;padding:1.9rem 1.7rem}
.logo.big{width:54px;height:54px;border-radius:16px;margin:0 auto .9rem}.logo.big .ic{width:26px;height:26px}
.center{text-align:center}.pwrow{display:flex;gap:.4rem}.pwrow input{flex:1}
.empty{text-align:center;padding:2.4rem 1rem}
table.ph{width:100%;border-collapse:collapse;font-size:.88rem;margin-top:.6rem}
.ph td{padding:.35rem .4rem;border-bottom:1px solid var(--line)}.ph code{font-family:ui-monospace,Menlo,monospace;color:var(--accent)}
details summary{cursor:pointer;font-weight:700}
.danger-zone{border-color:var(--danger-soft)}
.foot{margin-top:1.4rem;display:flex;gap:.6rem;align-items:center;justify-content:center;color:var(--muted);font-size:.8rem;flex-wrap:wrap}
.lang{display:flex;gap:.15rem}
.lang a{padding:.15rem .45rem;border-radius:6px;font-size:.75rem;font-weight:700;color:var(--muted)}
.lang a:hover{text-decoration:none;background:var(--accent-soft)}.lang a.cur{background:var(--accent-soft);color:var(--accent)}
@media(max-width:520px){.topbar .muted{display:none}.wrap{padding-top:1rem}}
"""

JS = """
(function(){
const L=window.L||{};
const $=(s,r=document)=>r.querySelector(s),$$=(s,r=document)=>[...r.querySelectorAll(s)];
function copy(t){if(navigator.clipboard&&window.isSecureContext)return navigator.clipboard.writeText(t);
return new Promise((ok,no)=>{const a=document.createElement('textarea');a.value=t;a.style.cssText='position:fixed;opacity:0';
document.body.appendChild(a);a.select();try{document.execCommand('copy')?ok():no()}catch(x){no(x)}a.remove()})}
function toast(m,err){const t=document.createElement('div');t.className='toast'+(err?' err':'');t.textContent=m;
document.body.appendChild(t);setTimeout(()=>t.classList.add('out'),2600);setTimeout(()=>t.remove(),3100)}
document.addEventListener('click',ev=>{
const c=ev.target.closest('[data-copy]');
if(c){ev.preventDefault();copy(c.dataset.copy).then(()=>toast(L.copied),()=>toast(L.copy_failed,1));return}
const o=ev.target.closest('[data-open]');
if(o){const d=document.getElementById(o.dataset.open);d.showModal?d.showModal():d.setAttribute('open','');const f=$('input[type=text]',d);f&&f.focus();return}
const x=ev.target.closest('[data-close]');if(x){x.closest('dialog').close();return}
const i=ev.target.closest('[data-insert]');
if(i){const t=document.getElementById('body');if(t){t.setRangeText(i.dataset.insert,t.selectionStart,t.selectionEnd,'end');t.focus()}return}
const p=ev.target.closest('[data-preview]');
if(p){ev.preventDefault();const out=document.getElementById('preview');out.textContent='…';
fetch(p.dataset.preview,{credentials:'same-origin',cache:'no-store'}).then(r=>r.text()).then(t=>{out.textContent=t||L.empty}).catch(()=>{out.textContent=L.load_error})}
});
document.addEventListener('submit',ev=>{const m=ev.target.dataset.confirm;if(m&&!confirm(m))ev.preventDefault()});
const f=$('#filter');
if(f)f.addEventListener('input',()=>{const q=f.value.toLowerCase();$$('.item').forEach(r=>{r.hidden=!!q&&!r.dataset.text.toLowerCase().includes(q)})});
const fl=$('#flash');if(fl&&fl.dataset.text)toast(fl.dataset.text,fl.dataset.kind==='err');
const u=new URL(location.href);
if(u.searchParams.has('msg')||u.searchParams.has('err')){['msg','err','n'].forEach(k=>u.searchParams.delete(k));history.replaceState(null,'',u)}
const pw=$('#pw'),tg=$('#pwtoggle');
if(pw&&tg)tg.addEventListener('click',()=>{const s=pw.type==='password';pw.type=s?'text':'password';tg.textContent=s?L.hide:L.show});
})();
"""

_IC = {
    "logo": '<path d="M4 6h16M4 12h10M4 18h13"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "trash": '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
    "check": '<path d="M20 6 9 17l-5-5"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>',
    "ext": '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3"/>',
    "refresh": '<path d="M21 12a9 9 0 1 1-3-6.7L21 8M21 3v5h-5"/>',
    "back": '<path d="M19 12H5M12 19l-7-7 7-7"/>',
}


def icon(name):
    return (
        '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_IC[name]}</svg>'
    )


def e(s):
    return html.escape(str(s), quote=True)


class Ctx:
    def __init__(self, lang, csrf="", user="", base="", uid=0, path="/admin"):
        self.lang = lang
        self.csrf = csrf
        self.user = user
        self.base = base
        self.uid = uid
        self.path = path
        self.flash = None
        self.nonce = secrets.token_urlsafe(12)

    def t(self, key, **kw):
        return tr(self.lang, key, **kw)

    def csrf_field(self):
        return f"<input type='hidden' name='csrf' value='{e(self.csrf)}'>"


def lang_switch(ctx):
    items = ""
    for code in sorted(LOCALES):
        name = LOCALES[code].get("_name", code)
        cur = " class='cur'" if code == ctx.lang else ""
        items += f"<a{cur} href='/admin/lang/{code}?next={quote(ctx.path)}' hreflang='{code}' title='{e(name)}'>{code.upper()}</a>"
    return f"<nav class='lang' aria-label='{e(ctx.t('nav.language'))}'>{items}</nav>"


def layout(ctx, title, body, auth=False):
    flash = ""
    if ctx.flash:
        kind, text = ctx.flash
        flash = f"<div id='flash' hidden data-kind='{kind}' data-text='{e(text)}'></div>"
    labels = {k.split(".")[1]: ctx.t(k) for k in ("js.copied", "js.copy_failed", "js.load_error", "js.empty")}
    labels["show"], labels["hide"] = ctx.t("login.show"), ctx.t("login.hide")
    labels_js = json_js(labels)
    if auth:
        inner = f"<main class='auth'><div class='authbox'>{body}<div class='foot'>{lang_switch(ctx)}</div></div></main>"
    else:
        inner = (
            "<header class='topbar'><div class='in'>"
            f"<a class='brand' href='/admin'><span class='logo'>{icon('logo')}</span>{APP_NAME}</a><span class='sp'></span>"
            f"<a class='btn ghost sm' href='/admin/users'>{e(ctx.t('nav.users'))}</a><span class='muted small'>{e(ctx.user)}</span>"
            f"<form method='post' action='/admin/logout'>{ctx.csrf_field()}"
            f"<button class='btn ghost sm'>{icon('logout')}{e(ctx.t('nav.logout'))}</button></form></div></header>"
            f"<main class='wrap'>{body}<div class='foot'>{APP_NAME}{lang_switch(ctx)}</div></main>"
        )
    return (
        f"<!doctype html><html lang='{ctx.lang}'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta name='color-scheme' content='light dark'><meta name='robots' content='noindex,nofollow'>"
        f"<title>{e(title)} · {APP_NAME}</title><style>{CSS}</style></head><body>{inner}{flash}"
        f"<script nonce='{ctx.nonce}'>window.L={labels_js};</script>"
        f"<script nonce='{ctx.nonce}'>{JS}</script></body></html>"
    )


def json_js(obj):
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def badge(text, gray=False):
    return f"<span class='badge{' gray' if gray else ''}'>{e(text)}</span>"


def urlbox(ctx, text):
    return (
        f"<div class='urlbox'><code>{e(text)}</code>"
        f"<button type='button' class='btn ghost sm icon' data-copy='{e(text)}' title='{e(ctx.t('common.copy'))}' "
        f"aria-label='{e(ctx.t('common.copy'))}'>{icon('copy')}</button></div>"
    )


HELP = [
    ("{date}", "help.date"),
    ("{time}", "help.time"),
    ("{weekday}", "help.weekday"),
    ("{number:1-100}", "help.number"),
    ("{pick:a|b|c}", "help.pick"),
    ("{line:name}", "help.line"),
    ("{doc:name}", "help.doc"),
]


def help_table(ctx):
    rows = "".join(f"<tr><td><code>{e(k)}</code></td><td class='muted'>{e(ctx.t(d))}</td></tr>" for k, d in HELP)
    return f"<div class='card'><details><summary>{e(ctx.t('help.title'))}</summary><table class='ph'>{rows}</table></details></div>"


def chan_base(name):
    """Public base URL of a channel (subdomain mode) or '' when BASE_DOMAIN is not set."""
    return f"{PUBLIC_SCHEME}://{name}.{BASE_DOMAIN}" if BASE_DOMAIN else ""


def ep_urls(ctx, chan, slug):
    """(primary URL, path-mode URL)"""
    fallback = f"{ctx.base}/c/{chan}/{slug}"
    return (f"{chan_base(chan)}/{slug}" if BASE_DOMAIN else fallback), fallback


def view_login(ctx, error="", disabled=False):
    alert = f"<div class='alert' role='alert'>{e(error)}</div>" if error else ""
    if disabled:
        alert = f"<div class='alert'>{e(ctx.t('login.nousers', min=MIN_PW))}</div>"
    body = f"""<div class='card login'>
    <div class='center'><div class='logo big'>{icon('logo')}</div><h1>{APP_NAME}</h1>
    <p class='muted' style='margin-bottom:1.3rem'>{e(ctx.t('login.subtitle'))}</p></div>{alert}
    <form method='post' action='/admin/login'>
      <label class='f'>{e(ctx.t('login.username'))}<input type='text' name='user' autocomplete='username' autocapitalize='off' required autofocus maxlength='128'></label>
      <label class='f'>{e(ctx.t('login.password'))}<div class='pwrow'><input id='pw' type='password' name='password' autocomplete='current-password' required maxlength='512'>
      <button type='button' id='pwtoggle' class='btn ghost sm'>{e(ctx.t('login.show'))}</button></div></label>
      <button class='btn primary block'>{e(ctx.t('login.submit'))}</button>
    </form></div>"""
    return layout(ctx, ctx.t("login.title"), body, auth=True)


def view_channels(ctx, con):
    rows = con.execute(
        "SELECT c.*, (SELECT COUNT(*) FROM endpoints e WHERE e.channel_id=c.id) AS n FROM channels c "
        "ORDER BY lower(coalesce(nullif(c.title,''),c.name))"
    ).fetchall()
    cards = ""
    for r in rows:
        host = f"{r['name']}.{BASE_DOMAIN}" if BASE_DOMAIN else r["name"]
        url = f"{chan_base(r['name'])}/" if BASE_DOMAIN else f"{ctx.base}/c/{r['name']}/"
        count = ctx.t("ch.count_one") if r["n"] == 1 else ctx.t("ch.count_n", n=r["n"])
        cards += (
            f"<div class='card ep'><a class='t' href='/admin/c/{r['id']}'>{e(r['title'] or r['name'])}</a>"
            f"<div class='row'>{badge(host, True)}{badge(count)}</div>"
            f"{urlbox(ctx, url)}<div class='row'><a class='btn sm' href='/admin/c/{r['id']}'>{e(ctx.t('common.open'))}</a></div></div>"
        )
    if cards:
        cards = f"<div class='grid'>{cards}</div>"
    else:
        txt = ctx.t("ch.empty_sub" if BASE_DOMAIN else "ch.empty_path")
        cards = f"<div class='card empty'><h2>{e(ctx.t('ch.empty_title'))}</h2><p class='muted'>{e(txt)}</p></div>"
    suffix = f"<span class='muted'>.{e(BASE_DOMAIN)}</span>" if BASE_DOMAIN else ""
    label = ctx.t("ch.name_sub" if BASE_DOMAIN else "ch.name_path")
    dialog = f"""<dialog id='newch'><form method='post' action='/admin/c/new'>{ctx.csrf_field()}
      <h2>{e(ctx.t('ch.new'))}</h2>
      <label class='f'>{e(label)}
        <div class='pwrow' style='align-items:center'><input type='text' name='name' placeholder='mychannel' required
        pattern='[a-z0-9]([a-z0-9\\-]*[a-z0-9])?' maxlength='63' autocapitalize='off'>{suffix}</div></label>
      <label class='f'>{e(ctx.t('common.title_opt'))}<input type='text' name='title' maxlength='100'></label>
      <div class='row' style='justify-content:flex-end;margin-top:1rem'>
        <button type='button' class='btn ghost' data-close>{e(ctx.t('common.cancel'))}</button><button class='btn primary'>{e(ctx.t('common.create'))}</button></div>
    </form></dialog>"""
    note = "" if BASE_DOMAIN else f"<div class='card'><p class='muted small'>{e(ctx.t('ch.nobase'))}</p></div>"
    body = (
        f"<div class='head'><h1>{e(ctx.t('ch.title'))}</h1><span class='sp'></span>"
        f"<button class='btn primary' data-open='newch'>{icon('plus')}{e(ctx.t('ch.new'))}</button></div>{note}{cards}{dialog}"
    )
    return layout(ctx, ctx.t("ch.title"), body)


def view_channel(ctx, con, ch):
    rows = con.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM items i WHERE i.endpoint_id=e.id AND i.active=1) AS n "
        "FROM endpoints e WHERE e.channel_id=? ORDER BY lower(coalesce(nullif(title,''),slug))",
        (ch["id"],),
    ).fetchall()
    cards = ""
    for r in rows:
        url, _ = ep_urls(ctx, ch["name"], r["slug"])
        if r["kind"] == "document":
            badges = badge(ctx.t("ep.type_doc"))
        else:
            badges = badge(ctx.t("ep.type_list")) + badge(ctx.t("mode." + r["mode"]), True) + badge(ctx.t("ep.active_n", n=r["n"]), True)
        cards += (
            f"<div class='card ep'><a class='t' href='/admin/e/{r['id']}'>{e(r['title'] or r['slug'])}</a>"
            f"<div class='row'>{badges}</div>{urlbox(ctx, url)}"
            f"<div class='row'><a class='btn sm' href='/admin/e/{r['id']}'>{e(ctx.t('common.edit'))}</a>"
            f"<a class='btn ghost sm' href='{e(url)}' target='_blank' rel='noopener'>{icon('ext')}{e(ctx.t('common.open'))}</a></div></div>"
        )
    if cards:
        cards = f"<div class='grid'>{cards}</div>"
    else:
        cards = f"<div class='card empty'><h2>{e(ctx.t('ep.empty_title'))}</h2><p class='muted'>{e(ctx.t('ep.empty_text'))}</p></div>"
    host = f"{ch['name']}.{BASE_DOMAIN}" if BASE_DOMAIN else ch["name"]
    dialog = f"""<dialog id='new'><form method='post' action='/admin/e/new'>{ctx.csrf_field()}
      <input type='hidden' name='channel_id' value='{ch['id']}'>
      <h2>{e(ctx.t('chp.new_endpoint'))}</h2>
      <label class='f'>{e(ctx.t('ep.name_label'))}
        <input type='text' name='slug' placeholder='quotes' required pattern='[a-z0-9][a-z0-9_\\-]*' maxlength='64' autocapitalize='off'></label>
      <label class='f'>{e(ctx.t('common.title_opt'))}<input type='text' name='title' maxlength='100'></label>
      <div class='lbl'>{e(ctx.t('ep.type'))}</div>
      <div class='seg'><label><input type='radio' name='kind' value='list' checked><span>{e(ctx.t('ep.type_list'))}</span></label>
      <label><input type='radio' name='kind' value='document'><span>{e(ctx.t('ep.type_doc'))}</span></label></div>
      <p class='muted small'>{e(ctx.t('ep.type_help'))}</p>
      <div class='row' style='justify-content:flex-end;margin-top:1rem'>
        <button type='button' class='btn ghost' data-close>{e(ctx.t('common.cancel'))}</button><button class='btn primary'>{e(ctx.t('common.create'))}</button></div>
    </form></dialog>"""
    name_label = ctx.t("chp.name_sub" if BASE_DOMAIN else "chp.name_path")
    warn = ctx.t("chp.warn_sub" if BASE_DOMAIN else "chp.warn_path")
    settings = (
        f"<div class='card'><details><summary>{e(ctx.t('chp.settings'))}</summary>"
        f"<form method='post' action='/admin/c/{ch['id']}/save' style='margin-top:.9rem'>{ctx.csrf_field()}"
        f"<label class='f'>{e(name_label)}<input type='text' name='name' value='{e(ch['name'])}' required "
        f"pattern='[a-z0-9]([a-z0-9\\-]*[a-z0-9])?' maxlength='63'></label>"
        f"<label class='f'>{e(ctx.t('epv.title'))}<input type='text' name='title' value='{e(ch['title'])}' maxlength='100'></label>"
        f"<p class='muted small' style='margin-bottom:.8rem'>{e(warn)}</p>"
        f"<button class='btn primary sm'>{icon('check')}{e(ctx.t('common.save'))}</button></form>"
        f"<form method='post' action='/admin/c/{ch['id']}/delete' style='margin-top:1rem' "
        f"data-confirm='{e(ctx.t('chp.delete_confirm'))}'>{ctx.csrf_field()}"
        f"<button class='btn danger sm'>{icon('trash')}{e(ctx.t('chp.delete'))}</button></form></details></div>"
    )
    body = (
        f"<p><a href='/admin'>{icon('back')} {e(ctx.t('chp.back'))}</a></p>"
        f"<div class='head'><h1>{e(ch['title'] or ch['name'])}</h1>{badge(host, True)}<span class='sp'></span>"
        f"<button class='btn primary' data-open='new'>{icon('plus')}{e(ctx.t('chp.new_endpoint'))}</button></div>{cards}"
        f"{help_table(ctx)}{settings}{dialog}"
    )
    return layout(ctx, ch["title"] or ch["name"], body)


def bot_snippets(url):
    """(label, snippet) for common chat bots; the first one is shown prominently."""
    return [
        ("Streamlabs Cloudbot", "{readapi." + url + "}"),
        ("Streamlabs Chatbot", "$readapi(" + url + ")"),
        ("Nightbot", "$(urlfetch " + url + ")"),
        ("StreamElements", "${customapi." + url + "}"),
        ("Fossabot", "$(customapi " + url + ")"),
    ]


def view_endpoint(ctx, con, ep):
    url, fallback = ep_urls(ctx, ep["channel_name"], ep["slug"])
    snippets = bot_snippets(url)
    is_doc = ep["kind"] == "document"
    head = (
        f"<p><a href='/admin/c/{ep['channel_id']}'>{icon('back')} {e(ep['channel_title'] or ep['channel_name'])}</a></p>"
        f"<div class='head'><h1>{e(ep['title'] or ep['slug'])}</h1>{badge(ctx.t('ep.type_doc' if is_doc else 'ep.type_list'))}</div>"
    )
    alt = f"<div class='lbl'>{e(ctx.t('epv.alt'))}</div>{urlbox(ctx, fallback)}" if BASE_DOMAIN else ""
    others = "".join(f"<div class='lbl'>{e(n)}</div>{urlbox(ctx, s)}" for n, s in snippets[1:])
    access = (
        f"<div class='card'><h2>{e(ctx.t('epv.access'))}</h2><div class='lbl'>{e(ctx.t('epv.url'))}</div>{urlbox(ctx, url)}"
        f"<div class='lbl'>{e(snippets[0][0])}</div>{urlbox(ctx, snippets[0][1])}{alt}"
        f"<details style='margin-top:.9rem'><summary class='small'>{e(ctx.t('epv.bots_other'))}</summary>{others}</details>"
        f"<div class='row' style='margin-top:.9rem'><button class='btn sm' data-preview='/admin/preview/{ep['id']}'>{icon('refresh')}{e(ctx.t('epv.preview'))}</button>"
        f"<a class='btn ghost sm' href='{e(url)}' target='_blank' rel='noopener'>{icon('ext')}{e(ctx.t('epv.live'))}</a>"
        f"<span class='muted small'>{e(ctx.t('epv.preview_note'))}</span></div>"
        f"<pre class='pre' id='preview' aria-live='polite'>{e(ctx.t('epv.preview_hint'))}</pre></div>"
    )
    settings = (
        f"<div class='card'><h2>{e(ctx.t('epv.settings'))}</h2><form method='post' action='/admin/e/{ep['id']}/save'>{ctx.csrf_field()}"
        f"<label class='f'>{e(ctx.t('epv.title'))}<input type='text' name='title' value='{e(ep['title'])}' maxlength='100'></label>"
        f"<label class='f'>{e(ctx.t('epv.name_path'))}<input type='text' name='slug' value='{e(ep['slug'])}' pattern='[a-z0-9][a-z0-9_\\-]*' maxlength='64' required></label>"
    )
    if is_doc:
        chips = "".join(
            f"<button type='button' class='chip' data-insert='{e(k)}' title='{e(ctx.t(d))}'>{e(k)}</button>" for k, d in HELP
        )
        settings += (
            f"<div class='lbl'>{e(ctx.t('epv.insert'))}</div><div class='chips'>{chips}</div>"
            f"<label class='f'>{e(ctx.t('epv.text'))}<textarea id='body' name='body' spellcheck='false'>{e(ep['body'])}</textarea></label>"
        )
    else:
        seg = "".join(
            f"<label><input type='radio' name='mode' value='{k}'{' checked' if k == ep['mode'] else ''}><span>{e(ctx.t('mode.' + k))}</span></label>"
            for k in MODES
        )
        helps = " ".join(f"<b>{e(ctx.t('mode.' + k))}:</b> {e(ctx.t('mode.' + k + '_help'))}" for k in MODES)
        settings += (
            f"<div class='lbl'>{e(ctx.t('epv.mode'))}</div><div class='seg'>{seg}</div>"
            f"<p class='muted small' style='margin-bottom:1rem'>{helps}</p>"
        )
    settings += f"<button class='btn primary'>{icon('check')}{e(ctx.t('common.save'))}</button></form></div>"

    items = ""
    if not is_doc:
        rows = con.execute("SELECT * FROM items WHERE endpoint_id=? ORDER BY id", (ep["id"],)).fetchall()
        lst = ""
        for r in rows:
            on = bool(r["active"])
            tlabel = ctx.t("item.disable" if on else "item.enable")
            lst += (
                f"<div class='item{'' if on else ' off'}' data-text='{e(r['text'])}'>"
                f"<form class='grow' method='post' action='/admin/i/{r['id']}/save'>{ctx.csrf_field()}"
                f"<input type='text' name='text' value='{e(r['text'])}' maxlength='1000' aria-label='{e(ctx.t('item.entry'))}'>"
                f"<button class='btn ghost icon' title='{e(ctx.t('common.save'))}' aria-label='{e(ctx.t('common.save'))}'>{icon('check')}</button></form>"
                f"<form method='post' action='/admin/i/{r['id']}/toggle'>{ctx.csrf_field()}"
                f"<button class='switch{' on' if on else ''}' title='{e(tlabel)}' "
                f"aria-label='{e(ctx.t('item.toggle'))}' aria-pressed='{'true' if on else 'false'}'><i></i></button></form>"
                f"<form method='post' action='/admin/i/{r['id']}/delete' data-confirm='{e(ctx.t('item.delete_confirm'))}'>{ctx.csrf_field()}"
                f"<button class='btn ghost icon danger' title='{e(ctx.t('common.delete'))}' aria-label='{e(ctx.t('common.delete'))}'>{icon('trash')}</button></form></div>"
            )
        if not lst:
            lst = f"<p class='muted'>{e(ctx.t('epv.no_entries'))}</p>"
        flt = (
            f"<input type='text' id='filter' placeholder='{e(ctx.t('epv.filter'))}' style='max-width:260px' aria-label='{e(ctx.t('epv.filter'))}'>"
            if rows
            else ""
        )
        example = e(ctx.t("epv.example_lines")).replace("\n", "&#10;")
        items = (
            f"<div class='card'><div class='head' style='margin-bottom:.6rem'><h2 style='margin:0'>{e(ctx.t('epv.entries', n=len(rows)))}</h2><span class='sp'></span>{flt}</div>{lst}</div>"
            f"<div class='card'><h2>{e(ctx.t('epv.add_entries'))}</h2><form method='post' action='/admin/e/{ep['id']}/add'>{ctx.csrf_field()}"
            f"<label class='f'>{e(ctx.t('epv.one_per_line'))}<textarea name='lines' placeholder='{example}' style='min-height:7rem'></textarea></label>"
            f"<button class='btn primary'>{icon('plus')}{e(ctx.t('common.add'))}</button></form></div>"
        )
    danger = (
        f"<div class='card danger-zone'><h2>{e(ctx.t('epv.danger'))}</h2><form method='post' action='/admin/e/{ep['id']}/delete' "
        f"data-confirm='{e(ctx.t('epv.delete_confirm'))}'>{ctx.csrf_field()}"
        f"<button class='btn danger'>{icon('trash')}{e(ctx.t('epv.delete'))}</button></form></div>"
    )
    return layout(ctx, ep["title"] or ep["slug"], head + access + settings + items + danger)


def view_users(ctx, con):
    users = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    pw_attrs = f"required minlength='{MIN_PW}' maxlength='256'"
    cards = ""
    for u in users:
        me = u["id"] == ctx.uid
        created = ctx.t("usr.created", date=time.strftime(DATE_FORMAT, time.localtime(u["created"])))
        cur = (
            f"<label class='f'>{e(ctx.t('usr.current'))}<input type='password' name='current' "
            "autocomplete='current-password' required maxlength='512'></label>"
            if me
            else ""
        )
        dele = ""
        if not me and len(users) > 1:
            dele = (
                f"<form method='post' action='/admin/users/{u['id']}/delete' data-confirm='{e(ctx.t('usr.delete_confirm'))}'>"
                f"{ctx.csrf_field()}<button class='btn ghost sm danger'>{icon('trash')}{e(ctx.t('common.delete'))}</button></form>"
            )
        cards += (
            f"<div class='card'><div class='row'><b>{e(u['username'])}</b>{badge(ctx.t('common.you')) if me else ''}"
            f"<span class='muted small'>{e(created)}</span><span class='sp'></span>{dele}</div>"
            f"<details style='margin-top:.7rem'><summary class='small'>{e(ctx.t('usr.change_pw'))}</summary>"
            f"<form method='post' action='/admin/users/{u['id']}/password' style='margin-top:.8rem'>{ctx.csrf_field()}{cur}"
            f"<label class='f'>{e(ctx.t('usr.new'))}<input type='password' name='password' autocomplete='new-password' {pw_attrs}></label>"
            f"<label class='f'>{e(ctx.t('usr.repeat_new'))}<input type='password' name='password2' autocomplete='new-password' {pw_attrs}></label>"
            f"<button class='btn primary sm'>{icon('check')}{e(ctx.t('usr.save_pw'))}</button></form></details></div>"
        )
    add = (
        f"<div class='card'><h2>{e(ctx.t('usr.add_title'))}</h2><form method='post' action='/admin/users/new'>{ctx.csrf_field()}"
        f"<label class='f'>{e(ctx.t('login.username'))}<input type='text' name='username' autocomplete='off' autocapitalize='off' required "
        f"pattern='[A-Za-z0-9._\\-]{{3,32}}' maxlength='32' placeholder='{e(ctx.t('usr.name_hint'))}'></label>"
        f"<label class='f'>{e(ctx.t('login.password'))}<input type='password' name='password' autocomplete='new-password' {pw_attrs}></label>"
        f"<label class='f'>{e(ctx.t('usr.repeat_pw'))}<input type='password' name='password2' autocomplete='new-password' {pw_attrs}></label>"
        f"<button class='btn primary'>{icon('plus')}{e(ctx.t('usr.add_title'))}</button></form>"
        f"<p class='muted small' style='margin-top:.8rem'>{e(ctx.t('usr.note', min=MIN_PW))}</p></div>"
    )
    body = f"<div class='head'><h1>{e(ctx.t('nav.users'))}</h1>{badge(str(len(users)), True)}</div>{cards}{add}"
    return layout(ctx, ctx.t("nav.users"), body)


# ---------------------------------------------------------------- HTTP handler
class Handler(BaseHTTPRequestHandler):
    server_version = APP_NAME
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # --- helpers
    def lang(self):
        if getattr(self, "_lang", None):
            return self._lang
        code = None
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
            if LANG_COOKIE in c and c[LANG_COOKIE].value in LOCALES:
                code = c[LANG_COOKIE].value
        except Exception:
            pass
        if not code:
            for part in self.headers.get("Accept-Language", "").split(","):
                cand = part.split(";")[0].strip().lower()
                if cand in LOCALES:
                    code = cand
                    break
                if cand[:2] in LOCALES:
                    code = cand[:2]
                    break
        self._lang = code or DEFAULT_LANG
        return self._lang

    def t(self, key, **kw):
        return tr(self.lang(), key, **kw)

    def make_ctx(self, sess=None):
        path = urlparse(self.path).path
        if sess:
            ctx = Ctx(self.lang(), sess["csrf"], sess["username"], self.base_url(), sess["user_id"], path)
        else:
            ctx = Ctx(self.lang(), path=path)
        q = parse_qs(urlparse(self.path).query)
        for kind in ("err", "msg"):
            key = q.get(kind, [""])[0]
            full = f"{kind}.{key}"
            if key and full in LOCALES["en"]:
                n = re.sub(r"\D", "", q.get("n", [""])[0])[:6]
                ctx.flash = (kind, ctx.t(full, n=n, min=MIN_PW))
                break
        return ctx

    def is_https(self):
        if COOKIE_SECURE:
            return True
        return TRUST_PROXY and self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"

    def client_ip(self):
        if TRUST_PROXY:
            xff = self.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[-1].strip()
            if self.headers.get("X-Real-IP"):
                return self.headers["X-Real-IP"].strip()
        return self.client_address[0]

    def host_name(self):
        h = self.headers.get("Host", "")
        if TRUST_PROXY and self.headers.get("X-Forwarded-Host"):
            h = self.headers["X-Forwarded-Host"].split(",")[0]
        h = h.strip().lower()
        return h if h.startswith("[") else h.split(":")[0]

    def channel_host(self):
        """Channel name when the host is '<channel>.<BASE_DOMAIN>', else None (= admin / direct access)."""
        if not BASE_DOMAIN:
            return None
        host = self.host_name()
        if ADMIN_HOST and host == ADMIN_HOST:
            return None
        suffix = "." + BASE_DOMAIN
        return host[: -len(suffix)] if host.endswith(suffix) else None

    def base_url(self):
        pub = os.environ.get("PUBLIC_URL", "").rstrip("/")
        if pub:
            return pub
        host, scheme = self.headers.get("Host", "localhost"), "http"
        if TRUST_PROXY:
            host = self.headers.get("X-Forwarded-Host", host).split(",")[0].strip()
            scheme = self.headers.get("X-Forwarded-Proto", "http").split(",")[0].strip()
        return f"{scheme}://{host}"

    def send(self, code, body, ctype="text/plain; charset=utf-8", headers=None, nonce=None):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("Referrer-Policy", "no-referrer")
        if nonce:
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
            )
        else:
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        if self.is_https():
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        for k, v in headers or []:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def send_html(self, ctx, html_body, code=200, headers=None):
        self.send(code, html_body, "text/html; charset=utf-8", headers, nonce=ctx.nonce)

    def redirect(self, loc, headers=None):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in headers or []:
            self.send_header(k, v)
        self.end_headers()

    def fail(self, loc, key):
        return self.redirect(f"{loc}?err={key}")

    def ok(self, loc, key, n=None):
        return self.redirect(f"{loc}?msg={key}" + (f"&n={n}" if n is not None else ""))

    def cookie_header(self, name, value, max_age, http_only=True, samesite="Strict"):
        parts = [f"{name}={value}", "Path=/admin", f"SameSite={samesite}", f"Max-Age={max_age}"]
        if http_only:
            parts.append("HttpOnly")
        if self.is_https():
            parts.append("Secure")
        return ("Set-Cookie", "; ".join(parts))

    def session(self, con):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        m = c.get(COOKIE_NAME)
        if not m or not m.value:
            return None
        now = int(time.time())
        row = con.execute(
            "SELECT s.*, u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=? AND s.expires>?",
            (_sha(m.value), now),
        ).fetchone()
        if not row:
            return None
        exp = min(now + SESSION_TTL, row["created"] + SESSION_MAX)
        if exp - row["expires"] > 60:
            con.execute("UPDATE sessions SET expires=? WHERE id=?", (exp, row["id"]))
            con.commit()
        return row

    def cross_site(self):
        sfs = self.headers.get("Sec-Fetch-Site")
        return bool(sfs) and sfs not in ("same-origin", "none")

    def read_form(self, limit):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > limit:
            return None
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def not_found(self, public=False):
        return self.send(404, tr("en" if public else self.lang(), "http.not_found"))

    def serve_public(self, channel, slug):
        if not CHAN_RE.match(channel) or not SLUG_RE.match(slug):
            return self.not_found(True)
        con = db()
        try:
            ch = con.execute("SELECT id FROM channels WHERE name=?", (channel,)).fetchone()
            out = render_endpoint(con, ch["id"], slug) if ch else None
        finally:
            con.close()
        return self.not_found(True) if out is None else self.send(200, out)

    def caddy_ask(self):
        """Caddy on-demand TLS 'ask' endpoint: 200 only for existing channel subdomains (and ADMIN_HOST)."""
        dom = parse_qs(urlparse(self.path).query).get("domain", [""])[0].strip().lower()
        ok = bool(ADMIN_HOST) and dom == ADMIN_HOST
        suffix = "." + BASE_DOMAIN
        if not ok and BASE_DOMAIN and dom.endswith(suffix) and CHAN_RE.match(dom[: -len(suffix)]):
            con = db()
            try:
                ok = con.execute("SELECT 1 FROM channels WHERE name=?", (dom[: -len(suffix)],)).fetchone() is not None
            finally:
                con.close()
        return self.send(200 if ok else 404, "ok" if ok else "no")

    # --- GET
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        chan = self.channel_host()
        if chan is not None:  # <channel>.<BASE_DOMAIN>/<name>: public output only, no admin
            slug = path.strip("/")
            return self.send(200, "") if not slug else self.serve_public(chan, slug)
        if path == "/healthz":
            return self.send(200, "ok")
        if path == "/caddy-ask":
            return self.caddy_ask()
        m = re.fullmatch(r"/c/([^/]+)/([^/]+)/?", path)
        if m:
            return self.serve_public(m.group(1), m.group(2))
        if path == "/":
            return self.send(200, "")
        m = re.fullmatch(r"/admin/lang/([a-z0-9-]+)", path)
        if m:
            nxt = parse_qs(urlparse(self.path).query).get("next", ["/admin"])[0]
            if not re.fullmatch(r"/admin[A-Za-z0-9/_.\-]*", nxt):
                nxt = "/admin"
            hdrs = [self.cookie_header(LANG_COOKIE, m.group(1), 31536000, http_only=False, samesite="Lax")] if m.group(1) in LOCALES else []
            return self.redirect(nxt, hdrs)
        if not path.startswith("/admin"):
            return self.not_found()

        con = db()
        try:
            sess = self.session(con)
            if path == "/admin/login":
                if sess:
                    return self.redirect("/admin")
                ctx = self.make_ctx()
                no_users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
                return self.send_html(ctx, view_login(ctx, disabled=no_users))
            if not sess:
                return self.redirect("/admin/login")
            ctx = self.make_ctx(sess)
            if path in ("/admin", "/admin/"):
                return self.send_html(ctx, view_channels(ctx, con))
            if path == "/admin/users":
                return self.send_html(ctx, view_users(ctx, con))
            m = re.fullmatch(r"/admin/c/(\d+)", path)
            if m:
                ch = con.execute("SELECT * FROM channels WHERE id=?", (m.group(1),)).fetchone()
                if ch:
                    return self.send_html(ctx, view_channel(ctx, con, ch))
            m = re.fullmatch(r"/admin/e/(\d+)", path)
            if m:
                ep = con.execute(
                    "SELECT e.*, c.name AS channel_name, c.title AS channel_title "
                    "FROM endpoints e JOIN channels c ON c.id=e.channel_id WHERE e.id=?",
                    (m.group(1),),
                ).fetchone()
                if ep:
                    return self.send_html(ctx, view_endpoint(ctx, con, ep))
            m = re.fullmatch(r"/admin/preview/(\d+)", path)
            if m:
                ep = con.execute("SELECT slug, channel_id FROM endpoints WHERE id=?", (m.group(1),)).fetchone()
                if ep:
                    return self.send(200, render_endpoint(con, ep["channel_id"], ep["slug"], advance=False) or "")
        finally:
            con.close()
        return self.not_found()

    # --- POST
    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/admin") or self.channel_host() is not None:
            return self.not_found()
        if self.cross_site():
            return self.send(403, self.t("http.cross_site"))
        if path == "/admin/login":
            return self.handle_login()
        con = db()
        try:
            sess = self.session(con)
            if not sess:
                return self.redirect("/admin/login")
            form = self.read_form(2_000_000)
            if form is None:
                return self.send(413, self.t("http.too_large"))
            if not hmac.compare_digest(form.get("csrf", ""), sess["csrf"]):
                return self.send(403, self.t("http.csrf"))
            if path == "/admin/logout":
                con.execute("DELETE FROM sessions WHERE id=?", (sess["id"],))
                con.commit()
                return self.redirect("/admin/login", [self.cookie_header(COOKIE_NAME, "", 0)])
            self.route_post(con, path, form, sess)
        except sqlite3.IntegrityError:
            self.fail("/admin", "generic")
        finally:
            con.close()

    def handle_login(self):
        ctx = self.make_ctx()
        form = self.read_form(8192)
        if form is None:
            return self.send(413, self.t("http.too_large"))
        con = db()
        try:
            if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
                return self.send_html(ctx, view_login(ctx, disabled=True), 503)
            name = form.get("user", "").strip().lower()[:64]
            ip_key, user_key = "ip:" + self.client_ip(), "user:" + name
            remain = max(lock_remaining(ip_key), lock_remaining(user_key))
            if remain:
                return self.send_html(ctx, view_login(ctx, ctx.t("login.locked", n=math.ceil(remain / 60))), 429)
            row = verify_user(con, name, form.get("password", ""))
            if not row:
                register_fail(ip_key, MAX_FAILS)
                register_fail(user_key, MAX_FAILS * 2)
                time.sleep(1)
                return self.send_html(ctx, view_login(ctx, ctx.t("login.bad")), 401)
            clear_fails(ip_key, user_key)
            token = new_session(con, row["id"])
        finally:
            con.close()
        return self.redirect("/admin", [self.cookie_header(COOKIE_NAME, token, SESSION_TTL)])

    def route_post(self, con, path, form, sess):
        # --- channels
        if path == "/admin/c/new":
            name = form.get("name", "").strip().lower()
            if not CHAN_RE.match(name) or name in RESERVED_CHANNELS:
                return self.fail("/admin", "chan_invalid")
            try:
                cur = con.execute(
                    "INSERT INTO channels(name,title,created) VALUES(?,?,?)",
                    (name, form.get("title", "").strip()[:100], int(time.time())),
                )
            except sqlite3.IntegrityError:
                return self.fail("/admin", "chan_taken")
            con.commit()
            return self.ok(f"/admin/c/{cur.lastrowid}", "channel_created")

        m = re.fullmatch(r"/admin/c/(\d+)/(save|delete)", path)
        if m:
            cid, action = int(m.group(1)), m.group(2)
            ch = con.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
            if not ch:
                return self.not_found()
            if action == "delete":
                con.execute("DELETE FROM channels WHERE id=?", (cid,))
                con.commit()
                return self.ok("/admin", "channel_deleted")
            name = form.get("name", ch["name"]).strip().lower()
            loc = f"/admin/c/{cid}"
            if not CHAN_RE.match(name) or name in RESERVED_CHANNELS:
                return self.fail(loc, "chan_invalid")
            try:
                con.execute("UPDATE channels SET name=?, title=? WHERE id=?", (name, form.get("title", "").strip()[:100], cid))
            except sqlite3.IntegrityError:
                return self.fail(loc, "chan_taken")
            con.commit()
            return self.ok(loc, "saved")

        # --- endpoints
        if path == "/admin/e/new":
            cid = form.get("channel_id", "")
            ch = con.execute("SELECT id FROM channels WHERE id=?", (cid,)).fetchone() if cid.isdigit() else None
            if not ch:
                return self.fail("/admin", "chan_notfound")
            loc = f"/admin/c/{ch['id']}"
            slug = form.get("slug", "").strip().lower()
            if not SLUG_RE.match(slug):
                return self.fail(loc, "ep_invalid")
            kind = "document" if form.get("kind") == "document" else "list"
            try:
                cur = con.execute(
                    "INSERT INTO endpoints(channel_id,slug,title,kind) VALUES(?,?,?,?)",
                    (ch["id"], slug, form.get("title", "").strip()[:100], kind),
                )
            except sqlite3.IntegrityError:
                return self.fail(loc, "ep_taken")
            con.commit()
            return self.ok(f"/admin/e/{cur.lastrowid}", "endpoint_created")

        m = re.fullmatch(r"/admin/e/(\d+)/(save|delete|add)", path)
        if m:
            eid, action = int(m.group(1)), m.group(2)
            ep = con.execute("SELECT * FROM endpoints WHERE id=?", (eid,)).fetchone()
            if not ep:
                return self.not_found()
            loc = f"/admin/e/{eid}"
            if action == "delete":
                con.execute("DELETE FROM endpoints WHERE id=?", (eid,))
                con.commit()
                return self.ok(f"/admin/c/{ep['channel_id']}", "endpoint_deleted")
            if action == "save":
                slug = form.get("slug", ep["slug"]).strip().lower()
                if not SLUG_RE.match(slug):
                    return self.fail(loc, "ep_invalid")
                mode = form.get("mode", ep["mode"])
                mode = mode if mode in MODES else "random"
                body = form.get("body", ep["body"]).replace("\r\n", "\n")[:100_000]
                try:
                    con.execute(
                        "UPDATE endpoints SET slug=?,title=?,mode=?,body=? WHERE id=?",
                        (slug, form.get("title", "").strip()[:100], mode, body, eid),
                    )
                except sqlite3.IntegrityError:
                    return self.fail(loc, "ep_taken")
                con.commit()
                return self.ok(loc, "saved")
            if action == "add":
                lines = [l.strip()[:1000] for l in form.get("lines", "").replace("\r", "").split("\n") if l.strip()]
                con.executemany("INSERT INTO items(endpoint_id,text) VALUES(?,?)", [(eid, l) for l in lines])
                con.commit()
                return self.ok(loc, "added", len(lines))

        # --- items
        m = re.fullmatch(r"/admin/i/(\d+)/(save|toggle|delete)", path)
        if m:
            iid, action = int(m.group(1)), m.group(2)
            it = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
            if not it:
                return self.not_found()
            if action == "save" and form.get("text", "").strip():
                con.execute("UPDATE items SET text=? WHERE id=?", (form["text"].strip()[:1000], iid))
            elif action == "toggle":
                con.execute("UPDATE items SET active=1-active WHERE id=?", (iid,))
            elif action == "delete":
                con.execute("DELETE FROM items WHERE id=?", (iid,))
            con.commit()
            return self.redirect(f"/admin/e/{it['endpoint_id']}")

        # --- users
        if path == "/admin/users/new":
            name = form.get("username", "").strip().lower()
            if not USER_RE.match(name):
                return self.fail("/admin/users", "user_invalid")
            err = pw_error(form.get("password", ""), form.get("password2", ""))
            if err:
                return self.fail("/admin/users", err)
            salt, h = hash_pw(form["password"])
            try:
                con.execute(
                    "INSERT INTO users(username,pw_salt,pw_hash,created) VALUES(?,?,?,?)", (name, salt, h, int(time.time()))
                )
            except sqlite3.IntegrityError:
                return self.fail("/admin/users", "user_taken")
            con.commit()
            return self.ok("/admin/users", "user_created")

        m = re.fullmatch(r"/admin/users/(\d+)/(password|delete)", path)
        if m:
            uid, action = int(m.group(1)), m.group(2)
            u = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            if not u:
                return self.not_found()
            me = uid == sess["user_id"]
            if action == "delete":
                if me or con.execute("SELECT COUNT(*) FROM users").fetchone()[0] < 2:
                    return self.fail("/admin/users", "user_undeletable")
                con.execute("DELETE FROM users WHERE id=?", (uid,))
                con.commit()
                return self.ok("/admin/users", "user_deleted")
            if me and not verify_user(con, u["username"], form.get("current", "")):
                return self.fail("/admin/users", "pw_wrong")
            err = pw_error(form.get("password", ""), form.get("password2", ""))
            if err:
                return self.fail("/admin/users", err)
            salt, h = hash_pw(form["password"])
            con.execute("UPDATE users SET pw_salt=?, pw_hash=? WHERE id=?", (salt, h, uid))
            con.execute("DELETE FROM sessions WHERE user_id=? AND id!=?", (uid, sess["id"]))
            con.commit()
            return self.ok("/admin/users", "pw_changed")

        return self.not_found()


if __name__ == "__main__":
    init_db()
    print(f"{APP_NAME} listening on port {PORT} (languages: {', '.join(sorted(LOCALES))})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
