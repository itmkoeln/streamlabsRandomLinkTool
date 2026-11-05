# LineRoulette

[English](README.md) · [Deutsch](README.de.md) · [Français](README.fr.md) · [Español](README.es.md) · **Italiano**

Un piccolo server self-hosted che fornisce **endpoint di testo semplice per chatbot**: una riga casuale da una lista, righe a rotazione o un testo dinamico con segnaposto. Include un'interfaccia di amministrazione multiutente (English, Français, Español, Italiano, Deutsch) e **sottodomini per canale** facoltativi.

Molti chatbot sanno richiamare un URL e stampare la risposta, ma non sanno scegliere una *riga casuale* da una lista (ad esempio `{readapi.<url>}` di Streamlabs Cloudbot stampa sempre l'intera risposta). LineRoulette è il pezzo mancante: il bot chiama un URL e LineRoulette risponde con esattamente una riga.

- Semplice pagina bianca, `text/plain`, mai in cache: esattamente ciò che si aspettano i bot
- **Canali**: ogni streamer/community ha i propri endpoint (e, se si vuole, il proprio sottodominio)
- Liste (casuale / in ordine / tutte le righe) e documenti con segnaposto (`{date}`, `{weekday}`, `{number:1-100}`, `{pick:a|b|c}`, `{line:nome}`, `{doc:nome}`)
- Interfaccia di amministrazione: modulo di accesso, più utenti (stessi diritti), anteprima, pulsanti di copia con snippet per Streamlabs Cloudbot/Chatbot, Nightbot, StreamElements e Fossabot
- Sicurezza: password con hash scrypt, sessioni con cookie HttpOnly/SameSite, protezione CSRF, blocco anti brute-force, CSP rigorosa
- Solo libreria standard di Python, SQLite, una piccola immagine Docker

## Avvio rapido

```bash
cp .env.example .env        # imposta ADMIN_PASSWORD (minimo 10 caratteri)
docker compose up -d --build
```

Apri `http://localhost:8080/admin`, accedi come `admin` con la tua password, crea un **canale**, poi un **endpoint**, quindi aggiungi delle righe. L'amministrazione mostra l'URL e snippet pronti da copiare per i chatbot.

Senza Docker? Funziona anche `ADMIN_PASSWORD=... DB_PATH=./data.db python3 app.py` (Python 3.9+).

Portainer: incolla `examples/portainer-stack.yml` in *Stacks → Add stack → Web editor* (l'applicazione è incorporata; rigeneralo con `python3 scripts/make_portainer_stack.py`).

## Due modalità di URL (i sottodomini sono facoltativi)

| | Modalità percorso (predefinita) | Modalità sottodominio (facoltativa) |
|---|---|---|
| Attivazione | niente da fare | imposta `BASE_DOMAIN=hub.example.com` |
| URL pubblico | `https://hub.example.com/c/<canale>/<nome>` | `https://<canale>.hub.example.com/<nome>` |
| DNS | un record per `hub.example.com` | in più un record jolly `*.hub.example.com` (o uno per canale) |
| TLS | un certificato | certificato jolly **oppure** un certificato per canale |
| Admin | `https://hub.example.com/admin` | uguale, oppure su un host dedicato con `ADMIN_HOST=admin.hub.example.com` |

La modalità percorso continua sempre a funzionare, anche con la modalità sottodominio attiva. Sui sottodomini dei canali **viene servito solo l'output pubblico**: niente amministrazione e niente accesso. L'amministrazione è disponibile su tutti gli altri nomi host (il dominio base, `ADMIN_HOST`, l'IP del server).

I nomi dei canali sono etichette DNS: `a-z`, `0-9`, trattino (`www`, `admin`, `api` e `mail` sono riservati). Nomi degli endpoint: `a-z`, `0-9`, `_`, `-`.

## Reverse proxy

Dietro un proxy imposta sempre `TRUST_PROXY=1` (così il blocco degli accessi usa l'IP reale del client e i cookie ricevono l'attributo `Secure`). Esponi a Internet solo il proxy, non la porta di LineRoulette (firewall sulla porta pubblicata, oppure `expose` al posto di `ports`).

### Nginx Proxy Manager

1. **DNS:** record `A`/`AAAA` per `hub.example.com` → il tuo server. Modalità sottodominio: anche `*.hub.example.com` → il tuo server.
2. **Hosts → Proxy Hosts → Add Proxy Host**
   - *Domain Names*: `hub.example.com`. Modalità sottodominio: aggiungi `*.hub.example.com` (richiede un certificato con sfida DNS, vedi il passaggio 3) oppure elenca ogni sottodominio di canale, ad es. `miocanale.hub.example.com`.
   - *Scheme* `http` · *Forward Hostname / IP*: `lineroulette` se NPM condivide la rete Docker di LineRoulette, altrimenti l'IP del server · *Forward Port*: `8080` (o il tuo `HTTP_PORT`).
   - Attiva *Block Common Exploits*. I websocket non servono.
3. **Scheda SSL:** *Request a new SSL Certificate*, attiva *Force SSL* e *HTTP/2*, accetta i termini di Let's Encrypt.
   - Senza jolly: i nomi elencati vengono convalidati via HTTP (nient'altro da fare). Aggiungi ogni nuovo sottodominio di canale ai nomi di dominio del proxy host (un solo certificato li copre tutti).
   - Con `*.hub.example.com`: attiva *Use a DNS Challenge*, scegli il tuo provider DNS e incolla il suo token API.
4. **Impostazioni di LineRoulette** (`.env` o variabili di Portainer): `TRUST_PROXY=1` e, in modalità sottodominio, `BASE_DOMAIN=hub.example.com`. Facoltativo: `ADMIN_HOST=admin.hub.example.com` (aggiungi anche questo nome al proxy host).
5. **Test:** `curl https://hub.example.com/c/miocanale/frasi` (modalità percorso) oppure `curl https://miocanale.hub.example.com/frasi` (modalità sottodominio).

NPM inoltra l'intestazione `Host` per impostazione predefinita: LineRoulette ne ha bisogno per riconoscere il canale.

### Caddy

È incluso uno stack pronto con Caddy: `docker-compose.caddy.yml`. Imposta `DOMAIN` (e `BASE_DOMAIN` per la modalità sottodominio) in `.env`, scegli un Caddyfile con `CADDYFILE=` ed esegui `docker compose -f docker-compose.caddy.yml up -d --build`.

**Modalità percorso** – `examples/Caddyfile.path`:

```caddyfile
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
```

**Modalità sottodominio, un certificato per canale (consigliata, senza API DNS)** – `examples/Caddyfile.subdomain-ondemand`. DNS: `A`/`AAAA` per `hub.example.com` e `*.hub.example.com`. Caddy emette un certificato alla prima richiesta a un sottodominio di canale, ma solo dopo aver interpellato LineRoulette (`/caddy-ask`), che risponde *sì* soltanto per i canali esistenti e per `ADMIN_HOST`:

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

**Modalità sottodominio, un unico certificato jolly** – `examples/Caddyfile.subdomain-wildcard`. Richiede un Caddy compilato con il plugin del tuo provider DNS (`xcaddy build --with github.com/caddy-dns/cloudflare`, altri provider: github.com/caddy-dns) e un token API:

```caddyfile
{$DOMAIN}, *.{$DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy lineroulette:8080
}
```

I limiti di Let's Encrypt valgono per i certificati per canale; usa la variante jolly se crei molti canali.

## Snippet per chatbot

Sostituisci l'URL con il tuo (l'amministrazione mostra lo snippet esatto con un pulsante di copia):

| Bot | Snippet |
|---|---|
| Streamlabs Cloudbot | `{readapi.https://miocanale.hub.example.com/frasi}` |
| Streamlabs Chatbot | `$readapi(https://miocanale.hub.example.com/frasi)` |
| Nightbot | `$(urlfetch https://miocanale.hub.example.com/frasi)` |
| StreamElements | `${customapi.https://miocanale.hub.example.com/frasi}` |
| Fossabot | `$(customapi https://miocanale.hub.example.com/frasi)` |

Esempio di testo di un comando: `Timmy dice: {readapi.https://miocanale.hub.example.com/frasi}`. Il bot richiama l'URL a ogni comando, quindi ogni chiamata restituisce una riga diversa. Mantieni le righe brevi (limite della chat di Twitch: 500 caratteri).

## Configurazione

| Variabile | Predefinito | Significato |
|---|---|---|
| `ADMIN_PASSWORD` | – | **Obbligatoria al primo avvio** (min. 10 caratteri): crea il primo utente. Le modifiche successive vengono ignorate, salvo con `ADMIN_RESET=1`. |
| `ADMIN_USER` | `admin` | Nome del primo utente |
| `ADMIN_RESET` | `0` | `1` = reimposta la password di `ADMIN_USER` all'avvio (poi rimuovila) |
| `BASE_DOMAIN` | – | Attiva la modalità sottodominio (`<canale>.<BASE_DOMAIN>`) |
| `ADMIN_HOST` | – | Nome host che serve sempre l'interfaccia di amministrazione |
| `PUBLIC_SCHEME` | `https` | Schema degli URL mostrati in modalità sottodominio |
| `PUBLIC_URL` | – | URL di base pubblico mostrato in modalità percorso (predefinito: dedotto dalla richiesta) |
| `TRUST_PROXY` | `0` | `1` dietro un reverse proxy (`X-Forwarded-*`) |
| `COOKIE_SECURE` | `0` | `1` = imposta sempre il flag `Secure` sui cookie |
| `SESSION_HOURS` | `12` | Durata di una sessione inattiva (max. 7 giorni in totale) |
| `DEFAULT_LANG` | `en` | Lingua dell'interfaccia per i nuovi visitatori (`en`, `de`, `fr`, `es`, `it`); modificabile nel piè di pagina |
| `CONTENT_LANG` | `DEFAULT_LANG` | Lingua di `{weekday}` |
| `DATE_FORMAT` / `TIME_FORMAT` | `%Y-%m-%d` / `%H:%M` | Formato di `{date}` / `{time}` ([strftime](https://docs.python.org/3/library/time.html#time.strftime)) |
| `TZ` | `UTC` | Fuso orario, ad es. `Europe/Rome` |
| `HTTP_PORT` | `8080` | Porta dell'host (solo compose) |

## Note sulla sicurezza

- Password: scrypt con salt per utente. Le sessioni sono salvate lato server (con hash) e terminano con la disconnessione o con il cambio password.
- 5 accessi falliti per IP (10 per nome utente) bloccano per 10 minuti. Dietro un proxy serve `TRUST_PROXY=1`; se la porta dell'applicazione è raggiungibile anche direttamente, `X-Forwarded-For` può essere falsificato: chiudila con il firewall.
- Gli endpoint pubblici sono leggibili da chiunque conosca l'URL. Non inserire segreti nelle liste.
- Usa HTTPS (lo gestisce il proxy). Fai il backup del volume `lineroulette-data` (è un unico file SQLite).

## Gestione

- **Aggiornamento:** scarica i nuovi file ed esegui `docker compose up -d --build`. Il database viene migrato automaticamente.
- **Password dimenticata:** imposta `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_RESET=1`, riavvia una volta e poi rimuovi `ADMIN_RESET`.
- **Aggiungere una lingua:** copia `locales/en.json` in `locales/<codice>.json`, traduci i valori, imposta `_name` e riavvia. `python3 tests/check_locales.py` verifica tutte le chiavi.
- **Test:** `python3 tests/smoke_test.py` avvia da solo il server e verifica accesso, CSRF, canali, instradamento per host, lingue, utenti e migrazione.

## Licenza

MIT – vedi [LICENSE](LICENSE).
