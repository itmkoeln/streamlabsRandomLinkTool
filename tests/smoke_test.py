#!/usr/bin/env python3
"""End-to-end smoke test for LineRoulette (starts the server itself, standard library only).

Usage: python3 tests/smoke_test.py
"""
import contextlib
import html
import http.client
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOC = {os.path.basename(f)[:-5]: json.load(open(os.path.join(ROOT, "locales", f), encoding="utf-8"))
       for f in os.listdir(os.path.join(ROOT, "locales")) if f.endswith(".json")}
PW = "Correct-Horse-1"
results = {"ok": 0, "fail": 0}


def check(name, cond, extra=""):
    results["ok" if cond else "fail"] += 1
    print(("OK    " if cond else "FAIL  ") + name + ("" if cond else f"   {extra}"))


class Client:
    def __init__(self, port):
        self.port, self.cookies = port, {}

    def req(self, method, path, data=None, host=None, headers=None):
        h = dict(headers or {})
        if host:
            h["Host"] = host
        if self.cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in r.getheaders()}
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, val = v.split(";")[0].partition("=")
                if val == "":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        conn.close()
        return r.status, hdrs, text

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def post(self, path, data, **kw):
        return self.req("POST", path, data=data, **kw)

    def csrf(self):
        _, _, page = self.get("/admin")
        m = re.search(r"name='csrf' value='([^']+)'", page)
        return m.group(1) if m else ""


@contextlib.contextmanager
def server(port, tmp, **env):
    e = dict(os.environ, DB_PATH=os.path.join(tmp, "data.db"), PORT=str(port), TZ="UTC")
    for k in ("BASE_DOMAIN", "ADMIN_HOST", "TRUST_PROXY", "ADMIN_RESET", "DEFAULT_LANG", "CONTENT_LANG", "ADMIN_PASSWORD", "ADMIN_USER"):
        e.pop(k, None)
    e.update({k: str(v) for k, v in env.items()})
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "app.py")], env=e, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = Client(port)
    for _ in range(50):
        try:
            if c.get("/healthz")[0] == 200:
                break
        except OSError:
            time.sleep(0.2)
    try:
        yield c
    finally:
        p.terminate()
        p.wait(timeout=10)


def loc(r):  # redirect location
    return r[1].get("location", "")


def login(c, user="admin", pw=PW, **kw):
    return c.post("/admin/login", {"user": user, "password": pw}, **kw)


def run_subdomain_mode(tmp):
    print("\n=== subdomain mode (BASE_DOMAIN + ADMIN_HOST) ===")
    base, adm = "hub.example.test", "admin.hub.example.test"
    with server(18201, tmp, ADMIN_PASSWORD=PW, BASE_DOMAIN=base, ADMIN_HOST=adm, CONTENT_LANG="fr") as c:
        check("healthz", c.get("/healthz")[2] == "ok")
        r = c.get("/admin")
        check("admin redirects to login without session", r[0] == 303 and loc(r).endswith("/admin/login"))
        r = c.get("/admin/login")
        check("login page default language English", r[0] == 200 and LOC["en"]["login.subtitle"] in r[2])
        check("Accept-Language: fr", LOC["fr"]["login.subtitle"] in html.unescape(c.get("/admin/login", headers={"Accept-Language": "fr-CH,fr;q=0.9"})[2]))
        check("Accept-Language: it", LOC["it"]["login.subtitle"] in c.get("/admin/login", headers={"Accept-Language": "it"})[2])
        check("Accept-Language: unsupported falls back to English", LOC["en"]["login.subtitle"] in c.get("/admin/login", headers={"Accept-Language": "ja"})[2])
        r = c.get("/admin/lang/es?next=/admin/login")
        check("language switch sets cookie and redirects", r[0] == 303 and loc(r) == "/admin/login" and c.cookies.get("ph_lang") == "es")
        check("cookie language wins over Accept-Language", LOC["es"]["login.subtitle"] in c.get("/admin/login", headers={"Accept-Language": "fr"})[2])
        r = c.get("/admin/lang/de?next=//evil.example")
        check("open redirect blocked in language switch", loc(r) == "/admin")
        c.get("/admin/lang/en")
        check("wrong password -> 401", login(c, pw="wrong")[0] == 401)
        check("unknown user -> 401", login(c, user="nobody")[0] == 401)
        check("cross-site login rejected", login(c, headers={"Sec-Fetch-Site": "cross-site"})[0] == 403)
        r = login(c)
        check("login ok -> 303 + session cookie", r[0] == 303 and "ph_session" in c.cookies)
        sc = [v for k, v in [(k, v) for k, v in {"set-cookie": r[1].get("set-cookie", "")}.items()]][0]
        check("session cookie flags HttpOnly + SameSite=Strict", "HttpOnly" in sc and "SameSite=Strict" in sc)
        token = c.csrf()
        check("csrf token present", len(token) > 10)
        check("POST without CSRF -> 403", c.post("/admin/c/new", {"name": "x"})[0] == 403)
        check("POST with wrong CSRF -> 403", c.post("/admin/c/new", {"csrf": "bad", "name": "x"})[0] == 403)
        r = c.get("/admin")
        check("security headers on admin pages", "script-src 'nonce-" in r[1].get("content-security-policy", "") and r[1].get("x-frame-options") == "DENY")
        # --- channels
        P = lambda path, d: c.post(path, dict(d, csrf=token))
        for bad in ("admin", "my_channel", "-x", "WWW"):
            check(f"invalid/reserved channel name '{bad}' rejected", "err=chan_invalid" in loc(P("/admin/c/new", {"name": bad})))
        r = P("/admin/c/new", {"name": "Alpha", "title": "Alpha Channel"})
        check("channel 'alpha' created (case normalised)", "/admin/c/1" in loc(r) and "msg=channel_created" in loc(r))
        check("flash message rendered in English", html.escape(LOC["en"]["msg.channel_created"], quote=True) in c.get(loc(r))[2])
        P("/admin/c/new", {"name": "beta"})
        check("duplicate channel rejected", "err=chan_taken" in loc(P("/admin/c/new", {"name": "beta"})))
        # --- endpoints
        P("/admin/e/new", {"channel_id": 1, "slug": "quotes", "kind": "list"})
        P("/admin/e/new", {"channel_id": 2, "slug": "quotes", "kind": "list"})
        check("same endpoint name allowed in another channel", True)
        check("same name in same channel rejected", "err=ep_taken" in loc(P("/admin/e/new", {"channel_id": 1, "slug": "quotes", "kind": "list"})))
        check("invalid endpoint name rejected", "err=ep_invalid" in loc(P("/admin/e/new", {"channel_id": 1, "slug": "Bad Name", "kind": "list"})))
        check("unknown channel rejected", "err=chan_notfound" in loc(P("/admin/e/new", {"channel_id": 99, "slug": "x", "kind": "list"})))
        r = P("/admin/e/1/add", {"lines": "A1\nA2\nA3"})
        check("entries added with count in flash", "msg=added" in loc(r) and "n=3" in loc(r))
        P("/admin/e/2/add", {"lines": "B1\nB2"})
        P("/admin/e/new", {"channel_id": 1, "slug": "today", "kind": "document"})
        P("/admin/e/3/save", {"slug": "today", "title": "Today", "body": "{weekday} {line:quotes} {line:onlybeta} {number:5-5} {pick:z|z}"})
        P("/admin/e/new", {"channel_id": 2, "slug": "onlybeta", "kind": "list"})
        P("/admin/e/4/add", {"lines": "BETA-ONLY"})
        # --- public output
        gA = lambda p, h="alpha." + base: c2.get(p, host=h)
        c2 = Client(c.port)
        s, h, t = gA("/quotes")
        check("alpha.<base>/quotes -> text/plain from alpha", s == 200 and t in ("A1", "A2", "A3") and h["content-type"].startswith("text/plain"))
        check("beta.<base>/quotes -> beta content", gA("/quotes", "beta." + base)[2] in ("B1", "B2"))
        check("Host: upper case, port and trailing slash", gA("/quotes/", "ALPHA.Hub.Example.Test:8443")[2] in ("A1", "A2", "A3"))
        check("public response headers", h.get("x-content-type-options") == "nosniff" and "sandbox" in h.get("content-security-policy", "") and h.get("cache-control", "").startswith("no-store"))
        doc = gA("/today")[2]
        check("document: placeholders (french weekday, own-channel line, other channel NOT resolved)",
              doc.split(" ")[0] in LOC["fr"]["weekdays"] and "{line:onlybeta}" in doc and " 5 z" in doc, doc)
        check("unknown channel -> 404", gA("/quotes", "nope." + base)[0] == 404)
        check("unknown endpoint -> 404", gA("/nothing")[0] == 404)
        check("nested subdomain -> 404", gA("/quotes", "a.alpha." + base)[0] == 404)
        check("channel root -> 200 empty", gA("/")[0] == 200 and gA("/")[2] == "")
        check("admin NOT reachable on channel subdomain (GET)", gA("/admin")[0] == 404)
        check("login NOT reachable on channel subdomain (POST)", c2.post("/admin/login", {"user": "admin", "password": PW}, host="alpha." + base)[0] == 404)
        check("admin reachable on ADMIN_HOST", c2.get("/admin/login", host=adm)[0] == 200)
        check("admin reachable on plain host/IP", c2.get("/admin/login")[0] == 200)
        check("path mode /c/<channel>/<name> works too", c2.get("/c/beta/quotes")[2] in ("B1", "B2"))
        # --- caddy ask
        check("caddy-ask: existing channel -> 200", c2.get(f"/caddy-ask?domain=alpha.{base}")[0] == 200)
        check("caddy-ask: unknown channel -> 404", c2.get(f"/caddy-ask?domain=nope.{base}")[0] == 404)
        check("caddy-ask: foreign domain -> 404", c2.get("/caddy-ask?domain=alpha.evil.example")[0] == 404)
        check("caddy-ask: ADMIN_HOST -> 200", c2.get(f"/caddy-ask?domain={adm}")[0] == 200)
        check("caddy-ask not exposed on channel subdomain", c2.get("/caddy-ask?domain=x", host="alpha." + base)[0] == 404)
        # --- sequence mode + preview
        P("/admin/e/1/save", {"slug": "quotes", "title": "", "mode": "sequence"})
        seq = [gA("/quotes")[2] for _ in range(4)]
        check("sequence mode rotates", seq == ["A1", "A2", "A3", "A1"], seq)
        pv = c.get("/admin/preview/1")[2]
        check("preview shows next without advancing", pv == "A2" and gA("/quotes")[2] == "A2", pv)
        check("preview requires login", c2.get("/admin/preview/1")[0] == 303)
        # --- admin UI pages in every language + flash messages
        for code in sorted(LOC):
            c.get(f"/admin/lang/{code}?next=/admin")
            page = c.get("/admin")[2]
            check(f"[{code}] channels page translated", html.escape(LOC[code]["ch.title"], quote=True) in page)
            r = P("/admin/c/1/save", {"name": "alpha", "title": "Alpha Channel"})
            check(f"[{code}] flash message translated", html.escape(LOC[code]["msg.saved"], quote=True) in c.get(loc(r))[2])
            r = P("/admin/c/new", {"name": "admin"})
            expected = html.escape(LOC[code]["err.chan_invalid"], quote=True)
            check(f"[{code}] error message translated", expected in c.get(loc(r))[2])
        c.get("/admin/lang/en")
        for path in ("/admin", "/admin/c/1", "/admin/e/1", "/admin/e/3", "/admin/users"):
            check(f"page {path} renders", c.get(path)[0] == 200)
        page = c.get("/admin/e/1")[2]
        check("endpoint page shows subdomain URL and bot snippets",
              f"https://alpha.{base}/quotes" in page and "{readapi.https://alpha." in page and "$(urlfetch https://alpha." in page)
        # --- users
        check("short password rejected", "err=pw_short" in loc(P("/admin/users/new", {"username": "anna", "password": "short", "password2": "short"})))
        check("password mismatch rejected", "err=pw_mismatch" in loc(P("/admin/users/new", {"username": "anna", "password": PW, "password2": PW + "x"})))
        check("invalid username rejected", "err=user_invalid" in loc(P("/admin/users/new", {"username": "a b", "password": PW, "password2": PW})))
        check("user created", "msg=user_created" in loc(P("/admin/users/new", {"username": "Anna", "password": PW + "2", "password2": PW + "2"})))
        check("duplicate user rejected", "err=user_taken" in loc(P("/admin/users/new", {"username": "anna", "password": PW, "password2": PW})))
        ca = Client(c.port)
        check("second user can sign in", login(ca, "anna", PW + "2")[0] == 303)
        ta = ca.csrf()
        r = ca.post("/admin/e/1/delete", {"csrf": ta})
        check("second user has full rights (delete endpoint)", "msg=endpoint_deleted" in loc(r))
        check("own password change needs current password", "err=pw_wrong" in loc(ca.post("/admin/users/2/password", {"csrf": ta, "current": "bad", "password": PW + "3", "password2": PW + "3"})))
        check("own password change works", "msg=pw_changed" in loc(ca.post("/admin/users/2/password", {"csrf": ta, "current": PW + "2", "password": PW + "3", "password2": PW + "3"})))
        check("cannot delete yourself", "err=user_undeletable" in loc(ca.post("/admin/users/2/delete", {"csrf": ta})))
        check("admin resets other user's password, ending their sessions", "msg=pw_changed" in loc(P("/admin/users/2/password", {"password": PW + "4", "password2": PW + "4"})) and ca.get("/admin")[0] == 303)
        check("admin deletes other user", "msg=user_deleted" in loc(P("/admin/users/2/delete", {})))
        check("cannot delete the last user", "err=user_undeletable" in loc(P("/admin/users/1/delete", {})))
        # --- channel rename / delete
        P("/admin/c/2/save", {"name": "gamma", "title": ""})
        check("renamed channel: new subdomain active, old gone", c2.get("/quotes", host="gamma." + base)[0] == 200 and c2.get("/quotes", host="beta." + base)[0] == 404)
        P("/admin/c/2/delete", {})
        check("deleted channel -> 404", c2.get("/quotes", host="gamma." + base)[0] == 404)
        check("logout ends session", c.post("/admin/logout", {"csrf": token})[0] == 303 and c.get("/admin")[0] == 303)
        # --- brute force lock (last: locks this client's IP)
        cb = Client(c.port)
        for _ in range(5):
            login(cb, pw="x")
        r = login(cb)
        check("5 failed logins lock the IP (even with the right password)", r[0] == 429)


def run_path_mode(tmp):
    print("\n=== path mode (no BASE_DOMAIN) ===")
    with server(18202, tmp, ADMIN_PASSWORD=PW, ADMIN_USER="Owner", DEFAULT_LANG="it") as c:
        check("default language Italian", LOC["it"]["login.subtitle"] in c.get("/admin/login")[2])
        check("custom ADMIN_USER (case-insensitive)", login(c, "OWNER")[0] == 303)
        t = c.csrf()
        P = lambda path, d: c.post(path, dict(d, csrf=t))
        P("/admin/c/new", {"name": "mine"})
        P("/admin/e/new", {"channel_id": 1, "slug": "hello", "kind": "list"})
        P("/admin/e/1/add", {"lines": "Ciao"})
        check("/c/<channel>/<name> serves text", c.get("/c/mine/hello")[2] == "Ciao")
        check("subdomain Host is not treated as a channel host", Client(c.port).get("/admin/login", host="mine.example.test")[0] == 200)
        check("caddy-ask -> 404 without BASE_DOMAIN", c.get("/caddy-ask?domain=mine.example.test")[0] == 404)
        check("UI hints at path mode", html.escape(LOC["it"]["ch.nobase"], quote=True) in c.get("/admin")[2])
        page = c.get("/admin/e/1")[2]
        check("endpoint page shows path URL, no subdomain URL", "/c/mine/hello" in page and "Alternativa senza sottodominio" not in page)


def run_no_password(tmp):
    print("\n=== no ADMIN_PASSWORD / too short ===")
    for pw in ("", "short"):
        d = tempfile.mkdtemp(dir=tmp)
        with server(18203, d, ADMIN_PASSWORD=pw) as c:
            r = c.get("/admin/login")
            check(f"no user (password={pw!r}): login page explains it", html.escape(LOC["en"]["login.nousers"].format(min=10), quote=True) in r[2])
            check("login impossible", login(c, "admin", pw or "x")[0] == 503)


def run_reset_and_migration(tmp):
    print("\n=== ADMIN_RESET and migration of an old database ===")
    d = tempfile.mkdtemp(dir=tmp)
    with server(18204, d, ADMIN_PASSWORD=PW) as c:
        check("first start creates admin", login(c)[0] == 303)
    with server(18204, d, ADMIN_PASSWORD="A-different-pass-9", ADMIN_RESET=1) as c:
        check("ADMIN_RESET: new password works", login(c, pw="A-different-pass-9")[0] == 303)
    with server(18204, d, ADMIN_PASSWORD=PW) as c:
        check("without reset the env password is ignored afterwards", login(c, pw=PW)[0] == 401 and login(c, pw="A-different-pass-9")[0] == 303)
    d = tempfile.mkdtemp(dir=tmp)
    con = sqlite3.connect(os.path.join(d, "data.db"))
    con.executescript("""
    CREATE TABLE endpoints(id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE NOT NULL, title TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'list', mode TEXT NOT NULL DEFAULT 'random', body TEXT NOT NULL DEFAULT '', counter INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE items(id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE, text TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    INSERT INTO endpoints(slug) VALUES('old'); INSERT INTO items(endpoint_id,text) VALUES(1,'Legacy entry');""")
    con.commit()
    con.close()
    with server(18205, d, ADMIN_PASSWORD=PW) as c:
        check("old database migrated into channel 'default'", c.get("/c/default/old")[2] == "Legacy entry")


def main():
    if subprocess.call([sys.executable, os.path.join(ROOT, "tests", "check_locales.py")]) != 0:
        sys.exit("locale check failed")
    tmp = tempfile.mkdtemp(prefix="lineroulette-test-")
    try:
        for fn in (run_subdomain_mode, run_path_mode, run_no_password, run_reset_and_migration):
            d = tempfile.mkdtemp(dir=tmp)
            fn(d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{results['ok']} passed, {results['fail']} failed")
    sys.exit(1 if results["fail"] else 0)


if __name__ == "__main__":
    main()
