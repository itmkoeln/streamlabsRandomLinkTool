# LineRoulette

[English](README.md) · [Deutsch](README.de.md) · **Français** · [Español](README.es.md) · [Italiano](README.it.md)

Un petit serveur auto-hébergé qui fournit des **points d'accès en texte brut pour chatbots** : une ligne aléatoire d'une liste, des lignes en rotation ou un texte dynamique avec variables. Il inclut une interface d'administration multi-utilisateurs (English, Français, Español, Italiano, Deutsch) et des **sous-domaines par chaîne** facultatifs.

Beaucoup de chatbots savent appeler une URL et afficher la réponse, mais pas choisir une *ligne aléatoire* dans une liste (par exemple, `{readapi.<url>}` de Streamlabs Cloudbot affiche toujours toute la réponse). LineRoulette comble ce manque : le bot appelle une URL, LineRoulette répond avec exactement une ligne.

- Page blanche simple, `text/plain`, jamais mise en cache – exactement ce qu'attendent les bots
- **Chaînes** : chaque streamer/communauté a ses propres points d'accès (et, si on le souhaite, son propre sous-domaine)
- Listes (aléatoire / dans l'ordre / toutes les lignes) et documents avec variables (`{date}`, `{weekday}`, `{number:1-100}`, `{pick:a|b|c}`, `{line:nom}`, `{doc:nom}`)
- Interface d'administration : formulaire de connexion, plusieurs utilisateurs (mêmes droits), aperçu, boutons de copie avec extraits pour Streamlabs Cloudbot/Chatbot, Nightbot, StreamElements et Fossabot
- Sécurité : mots de passe hachés avec scrypt, sessions avec cookies HttpOnly/SameSite, protection CSRF, verrouillage anti-force brute, CSP stricte
- Bibliothèque standard de Python uniquement, SQLite, une petite image Docker

## Démarrage rapide

```bash
cp .env.example .env        # définir ADMIN_PASSWORD (10 caractères minimum)
docker compose up -d --build
```

Ouvrez `http://localhost:8080/admin`, connectez-vous avec `admin` et votre mot de passe, créez une **chaîne**, puis un **point d'accès**, puis ajoutez des lignes. L'administration affiche l'URL et des extraits prêts à copier pour les chatbots.

Sans Docker ? `ADMIN_PASSWORD=... DB_PATH=./data.db python3 app.py` fonctionne aussi (Python 3.9+).

Portainer : collez `examples/portainer-stack.yml` dans *Stacks → Add stack → Web editor* (l'application y est intégrée ; régénérez-la avec `python3 scripts/make_portainer_stack.py`).

## Deux modes d'URL (les sous-domaines sont facultatifs)

| | Mode chemin (par défaut) | Mode sous-domaine (facultatif) |
|---|---|---|
| Activation | rien à faire | définir `BASE_DOMAIN=hub.example.com` |
| URL publique | `https://hub.example.com/c/<chaîne>/<nom>` | `https://<chaîne>.hub.example.com/<nom>` |
| DNS | un enregistrement pour `hub.example.com` | en plus un enregistrement générique `*.hub.example.com` (ou un par chaîne) |
| TLS | un certificat | certificat générique **ou** un certificat par chaîne |
| Admin | `https://hub.example.com/admin` | idem, ou sur son propre hôte via `ADMIN_HOST=admin.hub.example.com` |

Le mode chemin continue toujours de fonctionner, même quand le mode sous-domaine est activé. Sur les sous-domaines de chaîne, **seule la sortie publique est servie** – ni administration ni connexion. L'administration est disponible sur tous les autres noms d'hôte (votre domaine de base, `ADMIN_HOST`, l'IP du serveur).

Les noms de chaîne sont des labels DNS : `a-z`, `0-9`, tiret (`www`, `admin`, `api`, `mail` sont réservés). Noms de points d'accès : `a-z`, `0-9`, `_`, `-`.

## Reverse proxy

Derrière un proxy, définissez toujours `TRUST_PROXY=1` (le verrouillage de connexion utilise alors la vraie IP du client et les cookies reçoivent l'attribut `Secure`). N'exposez que le proxy à Internet, pas le port de LineRoulette (pare-feu sur le port publié, ou `expose` au lieu de `ports`).

### Nginx Proxy Manager

1. **DNS :** enregistrement `A`/`AAAA` pour `hub.example.com` → votre serveur. Mode sous-domaine : aussi `*.hub.example.com` → votre serveur.
2. **Hosts → Proxy Hosts → Add Proxy Host**
   - *Domain Names* : `hub.example.com`. Mode sous-domaine : ajoutez `*.hub.example.com` (nécessite un certificat par défi DNS, voir l'étape 3) ou listez chaque sous-domaine de chaîne, par ex. `machaine.hub.example.com`.
   - *Scheme* `http` · *Forward Hostname / IP* : `lineroulette` si NPM partage le réseau Docker de LineRoulette, sinon l'IP du serveur · *Forward Port* : `8080` (ou votre `HTTP_PORT`).
   - Activez *Block Common Exploits*. Les websockets ne sont pas nécessaires.
3. **Onglet SSL :** *Request a new SSL Certificate*, activez *Force SSL* et *HTTP/2*, acceptez les conditions de Let's Encrypt.
   - Sans certificat générique : les noms listés sont validés en HTTP (rien d'autre à faire). Ajoutez chaque nouveau sous-domaine de chaîne aux noms de domaine du proxy host (un seul certificat couvre l'ensemble).
   - Avec `*.hub.example.com` : activez *Use a DNS Challenge*, choisissez votre fournisseur DNS et collez son jeton d'API.
4. **Réglages de LineRoulette** (`.env` ou variables Portainer) : `TRUST_PROXY=1` et, en mode sous-domaine, `BASE_DOMAIN=hub.example.com`. Facultatif : `ADMIN_HOST=admin.hub.example.com` (ajoutez aussi ce nom au proxy host).
5. **Test :** `curl https://hub.example.com/c/machaine/citations` (mode chemin) ou `curl https://machaine.hub.example.com/citations` (mode sous-domaine).

NPM transmet l'en-tête `Host` par défaut – LineRoulette en a besoin pour reconnaître la chaîne.

### Caddy

Une stack prête à l'emploi avec Caddy est fournie : `docker-compose.caddy.yml`. Définissez `DOMAIN` (et `BASE_DOMAIN` pour le mode sous-domaine) dans `.env`, choisissez un Caddyfile avec `CADDYFILE=` et lancez `docker compose -f docker-compose.caddy.yml up -d --build`.

**Mode chemin** – `examples/Caddyfile.path` :

```caddyfile
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
```

**Mode sous-domaine, un certificat par chaîne (recommandé, sans API DNS)** – `examples/Caddyfile.subdomain-ondemand`. DNS : `A`/`AAAA` pour `hub.example.com` et `*.hub.example.com`. Caddy émet un certificat à la première requête vers un sous-domaine de chaîne, mais seulement après avoir interrogé LineRoulette (`/caddy-ask`), qui répond *oui* uniquement pour les chaînes existantes et `ADMIN_HOST` :

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

**Mode sous-domaine, un seul certificat générique** – `examples/Caddyfile.subdomain-wildcard`. Nécessite un Caddy compilé avec le plugin de votre fournisseur DNS (`xcaddy build --with github.com/caddy-dns/cloudflare`, autres fournisseurs : github.com/caddy-dns) et un jeton d'API :

```caddyfile
{$DOMAIN}, *.{$DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy lineroulette:8080
}
```

Les limites de débit de Let's Encrypt s'appliquent aux certificats par chaîne ; utilisez la variante générique si vous créez beaucoup de chaînes.

## Extraits pour chatbots

Remplacez l'URL par la vôtre (l'administration affiche l'extrait exact avec un bouton de copie) :

| Bot | Extrait |
|---|---|
| Streamlabs Cloudbot | `{readapi.https://machaine.hub.example.com/citations}` |
| Streamlabs Chatbot | `$readapi(https://machaine.hub.example.com/citations)` |
| Nightbot | `$(urlfetch https://machaine.hub.example.com/citations)` |
| StreamElements | `${customapi.https://machaine.hub.example.com/citations}` |
| Fossabot | `$(customapi https://machaine.hub.example.com/citations)` |

Exemple de texte de commande : `Timmy dit : {readapi.https://machaine.hub.example.com/citations}`. Le bot appelle l'URL à chaque commande, donc chaque appel renvoie une ligne différente. Gardez les lignes courtes (limite du chat Twitch : 500 caractères).

## Configuration

| Variable | Défaut | Signification |
|---|---|---|
| `ADMIN_PASSWORD` | – | **Obligatoire au premier démarrage** (10 caractères min.) : crée le premier utilisateur. Les modifications ultérieures sont ignorées, sauf avec `ADMIN_RESET=1`. |
| `ADMIN_USER` | `admin` | Nom du premier utilisateur |
| `ADMIN_RESET` | `0` | `1` = réinitialise le mot de passe de `ADMIN_USER` au démarrage (à retirer ensuite) |
| `BASE_DOMAIN` | – | Active le mode sous-domaine (`<chaîne>.<BASE_DOMAIN>`) |
| `ADMIN_HOST` | – | Nom d'hôte qui sert toujours l'interface d'administration |
| `PUBLIC_SCHEME` | `https` | Schéma des URL affichées en mode sous-domaine |
| `PUBLIC_URL` | – | URL de base publique affichée en mode chemin (par défaut : déduite de la requête) |
| `TRUST_PROXY` | `0` | `1` derrière un reverse proxy (`X-Forwarded-*`) |
| `COOKIE_SECURE` | `0` | `1` = toujours définir l'attribut `Secure` des cookies |
| `SESSION_HOURS` | `12` | Durée d'inactivité d'une session (7 jours au maximum au total) |
| `DEFAULT_LANG` | `en` | Langue de l'interface pour les nouveaux visiteurs (`en`, `de`, `fr`, `es`, `it`) – modifiable dans le pied de page |
| `CONTENT_LANG` | `DEFAULT_LANG` | Langue de `{weekday}` |
| `DATE_FORMAT` / `TIME_FORMAT` | `%Y-%m-%d` / `%H:%M` | Format de `{date}` / `{time}` ([strftime](https://docs.python.org/3/library/time.html#time.strftime)) |
| `TZ` | `UTC` | Fuseau horaire, par ex. `Europe/Paris` |
| `HTTP_PORT` | `8080` | Port de l'hôte (compose uniquement) |

## Notes de sécurité

- Mots de passe : scrypt avec un sel par utilisateur. Les sessions sont stockées côté serveur (hachées) et prennent fin à la déconnexion ou au changement de mot de passe.
- 5 connexions échouées par IP (10 par nom d'utilisateur) bloquent pendant 10 minutes. Derrière un proxy, cela exige `TRUST_PROXY=1` ; si le port de l'application est aussi joignable directement, `X-Forwarded-For` peut être falsifié – filtrez-le avec le pare-feu.
- Les points d'accès publics sont lisibles par quiconque connaît l'URL. Ne mettez pas de secrets dans les listes.
- Utilisez HTTPS (le proxy le gère). Sauvegardez le volume `lineroulette-data` (c'est un unique fichier SQLite).

## Exploitation

- **Mise à jour :** récupérez les nouveaux fichiers, `docker compose up -d --build`. La base de données est migrée automatiquement.
- **Mot de passe oublié :** définissez `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_RESET=1`, redémarrez une fois, puis retirez `ADMIN_RESET`.
- **Ajouter une langue :** copiez `locales/en.json` vers `locales/<code>.json`, traduisez les valeurs, renseignez `_name`, redémarrez. `python3 tests/check_locales.py` vérifie toutes les clés.
- **Tests :** `python3 tests/smoke_test.py` démarre lui-même le serveur et vérifie la connexion, le CSRF, les chaînes, le routage par hôte, les langues, les utilisateurs et la migration.

## Licence

MIT – voir [LICENSE](LICENSE).
