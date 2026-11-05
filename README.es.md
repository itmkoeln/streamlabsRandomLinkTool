# LineRoulette

[English](README.md) · [Deutsch](README.de.md) · [Français](README.fr.md) · **Español** · [Italiano](README.it.md)

Un pequeño servidor autoalojado que ofrece **endpoints de texto plano para chatbots**: una línea aleatoria de una lista, líneas en rotación o un texto dinámico con marcadores. Incluye una interfaz de administración multiusuario (English, Français, Español, Italiano, Deutsch) y **subdominios por canal** opcionales.

Muchos chatbots pueden consultar una URL e imprimir la respuesta, pero no pueden elegir una *línea aleatoria* de una lista (por ejemplo, `{readapi.<url>}` de Streamlabs Cloudbot siempre imprime la respuesta completa). LineRoulette es la pieza que falta: el bot llama a una URL y LineRoulette responde con exactamente una línea.

- Página en blanco simple, `text/plain`, nunca en caché: justo lo que esperan los bots
- **Canales**: cada streamer/comunidad tiene sus propios endpoints (y, si se quiere, su propio subdominio)
- Listas (aleatoria / en orden / todas las líneas) y documentos con marcadores (`{date}`, `{weekday}`, `{number:1-100}`, `{pick:a|b|c}`, `{line:nombre}`, `{doc:nombre}`)
- Interfaz de administración: formulario de acceso, varios usuarios (mismos derechos), vista previa, botones de copia con fragmentos para Streamlabs Cloudbot/Chatbot, Nightbot, StreamElements y Fossabot
- Seguridad: contraseñas con hash scrypt, sesiones con cookies HttpOnly/SameSite, protección CSRF, bloqueo contra fuerza bruta, CSP estricta
- Solo la biblioteca estándar de Python, SQLite, una imagen Docker pequeña

## Inicio rápido

```bash
cp .env.example .env        # define ADMIN_PASSWORD (mínimo 10 caracteres)
docker compose up -d --build
```

Abre `http://localhost:8080/admin`, inicia sesión como `admin` con tu contraseña, crea un **canal**, después un **endpoint** y añade líneas. La administración muestra la URL y fragmentos listos para copiar para los chatbots.

¿Sin Docker? `ADMIN_PASSWORD=... DB_PATH=./data.db python3 app.py` también funciona (Python 3.9+).

Portainer: pega `examples/portainer-stack.yml` en *Stacks → Add stack → Web editor* (la aplicación va incluida; vuelve a generarlo con `python3 scripts/make_portainer_stack.py`).

## Dos modos de URL (los subdominios son opcionales)

| | Modo ruta (por defecto) | Modo subdominio (opcional) |
|---|---|---|
| Activar | nada que hacer | definir `BASE_DOMAIN=hub.example.com` |
| URL pública | `https://hub.example.com/c/<canal>/<nombre>` | `https://<canal>.hub.example.com/<nombre>` |
| DNS | un registro para `hub.example.com` | además un registro comodín `*.hub.example.com` (o uno por canal) |
| TLS | un certificado | certificado comodín **o** un certificado por canal |
| Admin | `https://hub.example.com/admin` | igual, o en su propio host con `ADMIN_HOST=admin.hub.example.com` |

El modo ruta sigue funcionando siempre, incluso con el modo subdominio activado. En los subdominios de canal **solo se sirve la salida pública**: ni administración ni acceso. La administración está disponible en cualquier otro nombre de host (tu dominio base, `ADMIN_HOST`, la IP del servidor).

Los nombres de canal son etiquetas DNS: `a-z`, `0-9`, guion (`www`, `admin`, `api` y `mail` están reservados). Nombres de endpoint: `a-z`, `0-9`, `_`, `-`.

## Proxy inverso

Detrás de un proxy, define siempre `TRUST_PROXY=1` (así el bloqueo de acceso usa la IP real del cliente y las cookies llevan `Secure`). Expón a Internet solo el proxy, no el puerto de LineRoulette (cortafuegos para el puerto publicado, o `expose` en lugar de `ports`).

### Nginx Proxy Manager

1. **DNS:** registro `A`/`AAAA` para `hub.example.com` → tu servidor. Modo subdominio: también `*.hub.example.com` → tu servidor.
2. **Hosts → Proxy Hosts → Add Proxy Host**
   - *Domain Names*: `hub.example.com`. Modo subdominio: añade `*.hub.example.com` (requiere un certificado con desafío DNS, ver el paso 3) o enumera cada subdominio de canal, p. ej. `micanal.hub.example.com`.
   - *Scheme* `http` · *Forward Hostname / IP*: `lineroulette` si NPM comparte la red Docker de LineRoulette, de lo contrario la IP del servidor · *Forward Port*: `8080` (o tu `HTTP_PORT`).
   - Activa *Block Common Exploits*. No hacen falta websockets.
3. **Pestaña SSL:** *Request a new SSL Certificate*, activa *Force SSL* y *HTTP/2*, acepta las condiciones de Let's Encrypt.
   - Sin comodín: los nombres listados se validan por HTTP (no hay que hacer nada más). Añade cada nuevo subdominio de canal a los nombres de dominio del proxy host (un solo certificado los cubre todos).
   - Con `*.hub.example.com`: activa *Use a DNS Challenge*, elige tu proveedor DNS y pega su token de API.
4. **Ajustes de LineRoulette** (`.env` o variables de Portainer): `TRUST_PROXY=1` y, en modo subdominio, `BASE_DOMAIN=hub.example.com`. Opcional: `ADMIN_HOST=admin.hub.example.com` (añade también ese nombre al proxy host).
5. **Prueba:** `curl https://hub.example.com/c/micanal/frases` (modo ruta) o `curl https://micanal.hub.example.com/frases` (modo subdominio).

NPM reenvía la cabecera `Host` por defecto; LineRoulette la necesita para reconocer el canal.

### Caddy

Se incluye un stack listo con Caddy: `docker-compose.caddy.yml`. Define `DOMAIN` (y `BASE_DOMAIN` para el modo subdominio) en `.env`, elige un Caddyfile con `CADDYFILE=` y ejecuta `docker compose -f docker-compose.caddy.yml up -d --build`.

**Modo ruta** – `examples/Caddyfile.path`:

```caddyfile
{$DOMAIN} {
	reverse_proxy lineroulette:8080
}
```

**Modo subdominio, un certificado por canal (recomendado, sin API de DNS)** – `examples/Caddyfile.subdomain-ondemand`. DNS: `A`/`AAAA` para `hub.example.com` y `*.hub.example.com`. Caddy emite un certificado en la primera petición a un subdominio de canal, pero solo después de preguntar a LineRoulette (`/caddy-ask`), que responde *sí* únicamente para canales existentes y `ADMIN_HOST`:

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

**Modo subdominio, un único certificado comodín** – `examples/Caddyfile.subdomain-wildcard`. Requiere un Caddy compilado con el plugin de tu proveedor DNS (`xcaddy build --with github.com/caddy-dns/cloudflare`, otros proveedores: github.com/caddy-dns) y un token de API:

```caddyfile
{$DOMAIN}, *.{$DOMAIN} {
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy lineroulette:8080
}
```

Los límites de Let's Encrypt afectan a los certificados por canal; usa la variante comodín si creas muchos canales.

## Fragmentos para chatbots

Sustituye la URL por la tuya (la administración muestra el fragmento exacto con un botón de copia):

| Bot | Fragmento |
|---|---|
| Streamlabs Cloudbot | `{readapi.https://micanal.hub.example.com/frases}` |
| Streamlabs Chatbot | `$readapi(https://micanal.hub.example.com/frases)` |
| Nightbot | `$(urlfetch https://micanal.hub.example.com/frases)` |
| StreamElements | `${customapi.https://micanal.hub.example.com/frases}` |
| Fossabot | `$(customapi https://micanal.hub.example.com/frases)` |

Ejemplo de texto de comando: `Timmy dice: {readapi.https://micanal.hub.example.com/frases}`. El bot consulta la URL en cada comando, así que cada llamada devuelve una línea distinta. Mantén las líneas cortas (límite del chat de Twitch: 500 caracteres).

## Configuración

| Variable | Valor por defecto | Significado |
|---|---|---|
| `ADMIN_PASSWORD` | – | **Obligatoria en el primer arranque** (mín. 10 caracteres): crea el primer usuario. Los cambios posteriores se ignoran salvo con `ADMIN_RESET=1`. |
| `ADMIN_USER` | `admin` | Nombre del primer usuario |
| `ADMIN_RESET` | `0` | `1` = restablece la contraseña de `ADMIN_USER` al arrancar (quítalo después) |
| `BASE_DOMAIN` | – | Activa el modo subdominio (`<canal>.<BASE_DOMAIN>`) |
| `ADMIN_HOST` | – | Nombre de host que siempre sirve la interfaz de administración |
| `PUBLIC_SCHEME` | `https` | Esquema de las URL mostradas en modo subdominio |
| `PUBLIC_URL` | – | URL base pública mostrada en modo ruta (por defecto: se detecta a partir de la petición) |
| `TRUST_PROXY` | `0` | `1` detrás de un proxy inverso (`X-Forwarded-*`) |
| `COOKIE_SECURE` | `0` | `1` = poner siempre la marca `Secure` en las cookies |
| `SESSION_HOURS` | `12` | Duración de una sesión inactiva (máx. 7 días en total) |
| `DEFAULT_LANG` | `en` | Idioma de la interfaz para visitantes nuevos (`en`, `de`, `fr`, `es`, `it`); se puede cambiar en el pie de página |
| `CONTENT_LANG` | `DEFAULT_LANG` | Idioma de `{weekday}` |
| `DATE_FORMAT` / `TIME_FORMAT` | `%Y-%m-%d` / `%H:%M` | Formato de `{date}` / `{time}` ([strftime](https://docs.python.org/3/library/time.html#time.strftime)) |
| `TZ` | `UTC` | Zona horaria, p. ej. `Europe/Madrid` |
| `HTTP_PORT` | `8080` | Puerto del host (solo compose) |

## Notas de seguridad

- Contraseñas: scrypt con sal por usuario. Las sesiones se guardan en el servidor (con hash) y terminan al cerrar sesión o cambiar la contraseña.
- 5 accesos fallidos por IP (10 por nombre de usuario) bloquean durante 10 minutos. Detrás de un proxy hace falta `TRUST_PROXY=1`; si el puerto de la aplicación también es accesible directamente, `X-Forwarded-For` puede falsificarse: ciérralo con el cortafuegos.
- Los endpoints públicos los puede leer cualquiera que conozca la URL. No pongas secretos en las listas.
- Usa HTTPS (lo termina el proxy). Haz copia de seguridad del volumen `lineroulette-data` (es un único archivo SQLite).

## Operación

- **Actualizar:** descarga los archivos nuevos y ejecuta `docker compose up -d --build`. La base de datos se migra automáticamente.
- **Contraseña olvidada:** define `ADMIN_USER`, `ADMIN_PASSWORD`, `ADMIN_RESET=1`, reinicia una vez y quita `ADMIN_RESET`.
- **Añadir un idioma:** copia `locales/en.json` a `locales/<código>.json`, traduce los valores, define `_name` y reinicia. `python3 tests/check_locales.py` verifica todas las claves.
- **Pruebas:** `python3 tests/smoke_test.py` arranca el servidor por sí mismo y comprueba acceso, CSRF, canales, enrutamiento por host, idiomas, usuarios y migración.

## Licencia

MIT – ver [LICENSE](LICENSE).
