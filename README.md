# LineRoulette

**English** · [Deutsch](README.de.md) · [Français](README.fr.md) · [Español](README.es.md) · [Italiano](README.it.md)

A tiny self-hosted server that serves **plain-text endpoints for chat bots**: a random line from a list, lines in rotation, or a dynamic text with placeholders. It comes with a multi-user admin interface (English, Français, Español, Italiano, Deutsch) and optional **per-channel subdomains**.

Many chat bots can fetch a URL and print the answer, but cannot pick a *random line* from a list (for example Streamlabs Cloudbot's `{readapi.<url>}` always prints the whole response). LineRoulette is the missing piece: the bot calls a URL, LineRoulette answers with exactly one line.

- Plain white page, `text/plain`, never cached – exactly what bots expect
- **Channels**: every streamer/community gets its own endpoints (and optionally its own subdomain)
- Lists (random / in order / all lines) and documents with placeholders (`{date}`, `{weekday}`, `{number:1-100}`, `{pick:a|b|c}`, `{line:name}`, `{doc:name}`)
- Admin interface: login form, several users (equal rights), preview, copy buttons with snippets for Streamlabs Cloudbot/Chatbot, Nightbot, StreamElements and Fossabot
- Security: scrypt password hashes, sessions with HttpOnly/SameSite cookies, CSRF protection, brute-force lock, strict CSP
- Python standard library only, SQLite, one small Docker image

## Quick start

```bash
cp .env.example .env        # set ADMIN_PASSWORD (min. 10 characters)
docker compose up -d --build
```

Open `http://localhost:8080/admin`, sign in as `admin` with your password, create a **channel**, then an **endpoint**, then add lines. The admin shows the URL and ready-to-copy chat bot snippets.

No Docker? `ADMIN_PASSWORD=... DB_PATH=./data.db python3 app.py` works too (Python 3.9+).

Portainer: paste `examples/portainer-stack.yml` into *Stacks → Add stack → Web editor* (the app is embedded; regenerate it with `python3 scripts/make_portainer_stack.py`).

## Two URL modes (subdomains are optional)

| | Path mode (default) | Subdomain mode (optional) |
|---|---|---|
| Enable | nothing to do | set `BASE_DOMAIN=hub.example.com` |
| Public URL | `https://hub.example.com/c/<channel>/<name>` | `https://<channel>.hub.example.com/<name>` |
| DNS | one record for `hub.example.com` | additionally a wildcard record `*.hub.example.com` (or one record per channel) |
| TLS | one certificate | wildcard certificate, **or** one certificate per channel |
| Admin | `https://hub.example.com/admin` | same, or on its own host via `ADMIN_HOST=admin.hub.example.com` |

Path mode always keeps working, even when subdomain mode is on. On channel subdomains **only the public output is served** – no admin, no login. The admin is available on every other host name (your base domain, `ADMIN_HOST`, the server IP).

Channel names are DNS labels: `a-z`, `0-9`, hyphen (`www`, `admin`, `api`, `mail` are reserved). Endpoint names: `a-z`, `0-9`, `_`, `-`.

## Reverse proxy

Always set `TRUST_PROXY=1` behind a proxy (it makes the login lock use the real client IP and enables `Secure` cookies). Only expose the proxy, not LineRoulette's port, to the internet (firewall the published port or use `expose` instead of `ports`).

### Nginx Proxy Manager

1. **DNS:** `A`/`AAAA` record for `hub.example.com` → your server. Subdomain mode: also `*.hub.example.com` → your server.
2. **Hosts → Proxy Hosts → Add Proxy Host**
   - *Domain Names*: `hub.example.com`. Subdomain mode: add `*.hub.example.com` (needs a DNS-challenge certificate, see step 3) or list every channel subdomain explicitly, e.g. `mychannel.hub.example.com`.
   - *Scheme* `http` · *Forward Hostname / IP*: `lineroulette` if NPM shares LineRoulette's Docker network, otherwise the server IP · *Forward Port*: `8080` (or your `HTTP_PORT`).
   - Enable *Block Common Exploits*. Websockets are not needed.
3. **SSL tab:** *Request a new SSL Certificate*, enable *Force SSL* and *HTTP/2*, accept the Let's Encrypt terms.
   - Without wildcard: listed names are validated over HTTP (nothing else to do). Add each new channel subdomain to the proxy host's domain names (one certificate covers all of them).
   - With `*.hub.example.com`: switch on *Use a DNS Challenge*, choose your DNS provider and paste its API token.
4. **LineRoulette settings** (`.env` or Portainer variables): `TRUST_PROXY=1`, and for subdomain mode `BASE_DOMAIN=hub.example.com`. Optional: `ADMIN_HOST=admin.hub.example.com` (add that name to the proxy host as well).
5. **Test:** `curl https://hub.example.com/c/mychannel/quotes` (path mode) or `curl https://mychannel.hub.example.com/quotes` (subdomain mode).

NPM forwards the `Host` header by default – LineRoulette needs it to recognise the channel.

### Caddy

A ready-made stack with Caddy is included: `docker-compose.caddy.yml`. Set `DOMAIN` (and `BASE_DOMAIN` for subdomain mode) in `.env`, pick a Caddyfile with `CADDYFILE=` and run `docker compose -f docker-compose.caddy.yml up -d --build`.

**Path mode** – `examples/Caddyfile.path`:

```caddyfile
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
```

**Subdomain mode, certificate per channel (recommended, no DNS API)** – `examples/Caddyfile.subdomain-ondemand`. DNS: `A`/`AAAA` for `hub.example.com` and `*.hub.example.com`. Caddy issues a certificate on the first request to a channel subdomain, but only after asking LineRoulette (`/caddy-ask`), which answers *yes* only for existing channels and `ADMIN_HOST`:

```caddyfile
{
	on_demand_tls {
		ask http://lineroulette:8080/caddy-ask
	}
}
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
https:// {
	tls {
		on_demand
	}
	reverse_proxy lineroulette:8080
}
```

**Subdomain mode, one wildcard certificate** – `examples/Caddyfile.subdomain-wildcard`. Needs a Caddy build with your DNS provider's plugin (`xcaddy build --with github.com/caddy-dns/cloudflare`, other providers: github.com/caddy-dns) and an API token:

```caddyfile
{$DOMAIN}, *.{$DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy lineroulette:8080
}
```

Let's Encrypt rate limits apply to per-channel certificates; use the wildcard variant if you create many channels.

## Chat bot snippets

Replace the URL with yours (the admin shows the exact snippet with a copy button):

| Bot | Snippet |
|---|---|
| Streamlabs Cloudbot | `{readapi.https://mychannel.hub.example.com/quotes}` |
| Streamlabs Chatbot | `$readapi(https://mychannel.hub.example.com/quotes)` |
| Nightbot | `$(urlfetch https://mychannel.hub.example.com/quotes)` |
| StreamElements | `${customapi.https://mychannel.hub.example.com/quotes}` |
| Fossabot | `$(customapi https://mychannel.hub.example.com/quotes)` |

Example command text: `Timmy says: {readapi.https://mychannel.hub.example.com/quotes}`. The bot fetches the URL on every command, so every call returns a different line. Keep lines short (Twitch chat limit: 500 characters).

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | – | **Required on first start** (min. 10 characters): creates the first user. Later changes are ignored unless `ADMIN_RESET=1`. |
| `ADMIN_USER` | `admin` | Name of the first user |
| `ADMIN_RESET` | `0` | `1` = reset the password of `ADMIN_USER` at startup (then remove it again) |
| `BASE_DOMAIN` | – | Enables subdomain mode (`<channel>.<BASE_DOMAIN>`) |
| `ADMIN_HOST` | – | Host name that always serves the admin interface |
| `PUBLIC_SCHEME` | `https` | Scheme of the URLs shown in subdomain mode |
| `PUBLIC_URL` | – | Public base URL shown in path mode (default: detected from the request) |
| `TRUST_PROXY` | `0` | `1` behind a reverse proxy (`X-Forwarded-*`) |
| `COOKIE_SECURE` | `0` | `1` = always set the `Secure` cookie flag |
| `SESSION_HOURS` | `12` | Idle session lifetime (max. 7 days in total) |
| `DEFAULT_LANG` | `en` | Interface language for new visitors (`en`, `de`, `fr`, `es`, `it`) – users can switch in the footer |
| `CONTENT_LANG` | `DEFAULT_LANG` | Language of `{weekday}` |
| `DATE_FORMAT` / `TIME_FORMAT` | `%Y-%m-%d` / `%H:%M` | Format of `{date}` / `{time}` ([strftime](https://docs.python.org/3/library/time.html#time.strftime)) |
| `TZ` | `UTC` | Time zone, e.g. `Europe/Paris` |
| `HTTP_PORT` | `8080` | Host port (compose only) |

## Security notes

- Passwords: scrypt with a salt per user. Sessions are stored server-side (hashed) and end on sign-out or password change.
- 5 failed sign-ins per IP (10 per user name) lock for 10 minutes. Behind a proxy this needs `TRUST_PROXY=1`; if the app port is also reachable directly, `X-Forwarded-For` can be forged – firewall it.
- Public endpoints are readable by anyone who knows the URL. Don't put secrets into lists.
- Use HTTPS (the proxy terminates it). Back up the `lineroulette-data` volume (it is one SQLite file).

## Operations

- **Update:** pull the new files, `docker compose up -d --build`. The database migrates automatically.
- **Forgot the password:** set `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_RESET=1`, restart once, remove `ADMIN_RESET`.
- **Add a language:** copy `locales/en.json` to `locales/<code>.json`, translate the values, set `_name`, restart. `python3 tests/check_locales.py` verifies all keys.
- **Tests:** `python3 tests/smoke_test.py` starts the server itself and checks login, CSRF, channels, host routing, languages, users and migration.

## License

MIT – see [LICENSE](LICENSE).
