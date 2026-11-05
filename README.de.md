# LineRoulette

[English](README.md) · **Deutsch** · [Français](README.fr.md) · [Español](README.es.md) · [Italiano](README.it.md)

Ein winziger, selbst gehosteter Server für **Plaintext-Endpunkte für Chatbots**: eine zufällige Zeile aus einer Liste, Zeilen der Reihe nach oder ein dynamischer Text mit Platzhaltern. Dazu gibt es eine Mehrbenutzer-Adminoberfläche (English, Deutsch, Français, Español, Italiano) und optionale **Subdomains pro Kanal**.

Viele Chatbots (z. B. in OBS-/Streamlabs-Setups) können eine URL abrufen und die Antwort ausgeben, aber keine *zufällige Zeile* aus einer Liste wählen – das `{readapi.<url>}` des Streamlabs Cloudbots gibt immer die komplette Antwort aus. LineRoulette ist das fehlende Stück: Der Bot ruft eine URL auf, LineRoulette antwortet mit genau einer Zeile.

- Schlichte weiße Seite, `text/plain`, nie gecacht – genau das, was Bots erwarten
- **Kanäle**: Jeder Streamer bzw. jede Community bekommt eigene Endpunkte (optional mit eigener Subdomain)
- Listen (zufällig / der Reihe nach / alle Zeilen) und Dokumente mit Platzhaltern (`{date}`, `{weekday}`, `{number:1-100}`, `{pick:a|b|c}`, `{line:name}`, `{doc:name}`)
- Adminoberfläche: Login, mehrere Nutzer (gleiche Rechte), Vorschau, Kopier-Buttons mit Snippets für Streamlabs Cloudbot/Chatbot, Nightbot, StreamElements und Fossabot
- Sicherheit: scrypt-Passwort-Hashes, Sessions mit HttpOnly-/SameSite-Cookies, CSRF-Schutz, Brute-Force-Sperre, strenge CSP
- Nur Python-Standardbibliothek, SQLite, ein kleines Docker-Image

## Schnellstart

```bash
cp .env.example .env        # ADMIN_PASSWORD setzen (mind. 10 Zeichen)
docker compose up -d --build
```

Öffne `http://localhost:8080/admin`, melde dich als `admin` mit deinem Passwort an, lege einen **Kanal** an, dann einen **Endpunkt** und füge Zeilen hinzu. Die Admin-Seite zeigt die URL und fertige Snippets zum Kopieren.

Ohne Docker? `ADMIN_PASSWORD=... DB_PATH=./data.db python3 app.py` funktioniert ebenfalls (Python 3.9+).

Portainer: `examples/portainer-stack.yml` unter *Stacks → Add stack → Web editor* einfügen (die App ist eingebettet; neu erzeugen mit `python3 scripts/make_portainer_stack.py`).

## Zwei URL-Modi (Subdomains sind optional)

| | Pfad-Modus (Standard) | Subdomain-Modus (optional) |
|---|---|---|
| Aktivieren | nichts zu tun | `BASE_DOMAIN=hub.example.com` setzen |
| Öffentliche URL | `https://hub.example.com/c/<kanal>/<name>` | `https://<kanal>.hub.example.com/<name>` |
| DNS | ein Eintrag für `hub.example.com` | zusätzlich Wildcard-Eintrag `*.hub.example.com` (oder ein Eintrag je Kanal) |
| TLS | ein Zertifikat | Wildcard-Zertifikat **oder** ein Zertifikat pro Kanal |
| Admin | `https://hub.example.com/admin` | genauso, oder auf eigenem Host über `ADMIN_HOST=admin.hub.example.com` |

Der Pfad-Modus funktioniert immer, auch wenn der Subdomain-Modus aktiv ist. Auf Kanal-Subdomains wird **ausschließlich die öffentliche Ausgabe** ausgeliefert – kein Admin, kein Login. Die Admin-Oberfläche ist auf jedem anderen Hostnamen erreichbar (Basisdomain, `ADMIN_HOST`, Server-IP).

Kanalnamen sind DNS-Labels: `a-z`, `0-9`, Bindestrich (`www`, `admin`, `api`, `mail` sind reserviert). Endpunktnamen: `a-z`, `0-9`, `_`, `-`.

## Reverse Proxy

Hinter einem Proxy immer `TRUST_PROXY=1` setzen (dann nutzt die Login-Sperre die echte Client-IP, und Cookies erhalten das `Secure`-Flag). Nur den Proxy ins Internet stellen, nicht den Port von LineRoulette (Firewall für den veröffentlichten Port oder `expose` statt `ports`).

### Nginx Proxy Manager

1. **DNS:** `A`/`AAAA`-Eintrag für `hub.example.com` → dein Server. Subdomain-Modus: zusätzlich `*.hub.example.com` → dein Server.
2. **Hosts → Proxy Hosts → Add Proxy Host**
   - *Domain Names*: `hub.example.com`. Subdomain-Modus: `*.hub.example.com` ergänzen (braucht ein Zertifikat per DNS-Challenge, siehe Schritt 3) oder jede Kanal-Subdomain einzeln eintragen, z. B. `meinkanal.hub.example.com`.
   - *Scheme* `http` · *Forward Hostname / IP*: `lineroulette`, wenn NPM im selben Docker-Netzwerk wie LineRoulette läuft, sonst die Server-IP · *Forward Port*: `8080` (oder dein `HTTP_PORT`).
   - *Block Common Exploits* aktivieren. Websockets werden nicht benötigt.
3. **Reiter SSL:** *Request a new SSL Certificate*, *Force SSL* und *HTTP/2* aktivieren, Let's-Encrypt-Bedingungen akzeptieren.
   - Ohne Wildcard: Die eingetragenen Namen werden per HTTP validiert (nichts weiter zu tun). Jede neue Kanal-Subdomain zu den Domain Names des Proxy Hosts hinzufügen (ein Zertifikat deckt alle ab).
   - Mit `*.hub.example.com`: *Use a DNS Challenge* einschalten, DNS-Anbieter wählen und dessen API-Token eintragen.
4. **LineRoulette-Einstellungen** (`.env` oder Portainer-Variablen): `TRUST_PROXY=1`, im Subdomain-Modus zusätzlich `BASE_DOMAIN=hub.example.com`. Optional: `ADMIN_HOST=admin.hub.example.com` (diesen Namen ebenfalls im Proxy Host eintragen).
5. **Test:** `curl https://hub.example.com/c/meinkanal/sprueche` (Pfad-Modus) oder `curl https://meinkanal.hub.example.com/sprueche` (Subdomain-Modus).

NPM reicht den `Host`-Header standardmäßig weiter – LineRoulette braucht ihn, um den Kanal zu erkennen.

### Caddy

Ein fertiger Stack mit Caddy liegt bei: `docker-compose.caddy.yml`. In der `.env` `DOMAIN` (und für den Subdomain-Modus `BASE_DOMAIN`) setzen, mit `CADDYFILE=` ein Caddyfile wählen und `docker compose -f docker-compose.caddy.yml up -d --build` ausführen.

**Pfad-Modus** – `examples/Caddyfile.path`:

```caddyfile
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
```

**Subdomain-Modus, Zertifikat pro Kanal (empfohlen, keine DNS-API nötig)** – `examples/Caddyfile.subdomain-ondemand`. DNS: `A`/`AAAA` für `hub.example.com` und `*.hub.example.com`. Caddy stellt beim ersten Aufruf einer Kanal-Subdomain ein Zertifikat aus, fragt aber vorher LineRoulette (`/caddy-ask`), das nur für existierende Kanäle und `ADMIN_HOST` mit *ja* antwortet:

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

**Subdomain-Modus, ein Wildcard-Zertifikat** – `examples/Caddyfile.subdomain-wildcard`. Braucht einen Caddy-Build mit dem Plugin deines DNS-Anbieters (`xcaddy build --with github.com/caddy-dns/cloudflare`, weitere Anbieter: github.com/caddy-dns) und ein API-Token:

```caddyfile
{$DOMAIN}, *.{$DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy lineroulette:8080
}
```

Für Zertifikate pro Kanal gelten die Rate-Limits von Let's Encrypt; bei vielen Kanälen lieber die Wildcard-Variante nehmen.

## Chatbot-Snippets

URL durch die eigene ersetzen (die Adminoberfläche zeigt das exakte Snippet mit Kopier-Button):

| Bot | Snippet |
|---|---|
| Streamlabs Cloudbot | `{readapi.https://meinkanal.hub.example.com/sprueche}` |
| Streamlabs Chatbot | `$readapi(https://meinkanal.hub.example.com/sprueche)` |
| Nightbot | `$(urlfetch https://meinkanal.hub.example.com/sprueche)` |
| StreamElements | `${customapi.https://meinkanal.hub.example.com/sprueche}` |
| Fossabot | `$(customapi https://meinkanal.hub.example.com/sprueche)` |

Beispiel für einen Befehlstext: `Timmy sagt: {readapi.https://meinkanal.hub.example.com/sprueche}`. Der Bot ruft die URL bei jedem Befehl neu ab, daher liefert jeder Aufruf eine andere Zeile. Zeilen kurz halten (Twitch-Chat-Limit: 500 Zeichen).

## Konfiguration

| Variable | Standard | Bedeutung |
|---|---|---|
| `ADMIN_PASSWORD` | – | **Beim ersten Start erforderlich** (mind. 10 Zeichen): legt den ersten Nutzer an. Spätere Änderungen werden ignoriert, außer mit `ADMIN_RESET=1`. |
| `ADMIN_USER` | `admin` | Name des ersten Nutzers |
| `ADMIN_RESET` | `0` | `1` = Passwort von `ADMIN_USER` beim Start zurücksetzen (danach wieder entfernen) |
| `BASE_DOMAIN` | – | Aktiviert den Subdomain-Modus (`<kanal>.<BASE_DOMAIN>`) |
| `ADMIN_HOST` | – | Hostname, auf dem immer die Adminoberfläche ausgeliefert wird |
| `PUBLIC_SCHEME` | `https` | Schema der im Subdomain-Modus angezeigten URLs |
| `PUBLIC_URL` | – | Öffentliche Basis-URL im Pfad-Modus (Standard: aus der Anfrage erkannt) |
| `TRUST_PROXY` | `0` | `1` hinter einem Reverse Proxy (`X-Forwarded-*`) |
| `COOKIE_SECURE` | `0` | `1` = Cookies immer mit `Secure`-Flag setzen |
| `SESSION_HOURS` | `12` | Leerlaufdauer einer Sitzung (insgesamt max. 7 Tage) |
| `DEFAULT_LANG` | `en` | Oberflächensprache für neue Besucher (`en`, `de`, `fr`, `es`, `it`) – im Footer umschaltbar |
| `CONTENT_LANG` | `DEFAULT_LANG` | Sprache für `{weekday}` |
| `DATE_FORMAT` / `TIME_FORMAT` | `%Y-%m-%d` / `%H:%M` | Format von `{date}` / `{time}` ([strftime](https://docs.python.org/3/library/time.html#time.strftime)) |
| `TZ` | `UTC` | Zeitzone, z. B. `Europe/Berlin` |
| `HTTP_PORT` | `8080` | Host-Port (nur Compose) |

## Sicherheitshinweise

- Passwörter: scrypt mit Salt pro Nutzer. Sessions liegen serverseitig (gehasht) und enden bei Abmeldung oder Passwortänderung.
- 5 fehlgeschlagene Logins pro IP (10 pro Benutzername) sperren für 10 Minuten. Hinter einem Proxy braucht das `TRUST_PROXY=1`; ist der App-Port zusätzlich direkt erreichbar, lässt sich `X-Forwarded-For` fälschen – per Firewall schließen.
- Öffentliche Endpunkte kann jeder lesen, der die URL kennt. Keine Geheimnisse in Listen ablegen.
- HTTPS verwenden (der Proxy terminiert es). Das Volume `lineroulette-data` sichern (es ist eine einzelne SQLite-Datei).

## Betrieb

- **Update:** neue Dateien holen, `docker compose up -d --build`. Die Datenbank wird automatisch migriert.
- **Passwort vergessen:** `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_RESET=1` setzen, einmal neu starten, `ADMIN_RESET` wieder entfernen.
- **Sprache hinzufügen:** `locales/en.json` nach `locales/<code>.json` kopieren, Werte übersetzen, `_name` setzen, neu starten. `python3 tests/check_locales.py` prüft alle Schlüssel.
- **Tests:** `python3 tests/smoke_test.py` startet den Server selbst und prüft Login, CSRF, Kanäle, Host-Routing, Sprachen, Nutzer und Migration.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
