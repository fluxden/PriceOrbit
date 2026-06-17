# Environment variables

Every setting below is provided through the container environment. The bundled
[`docker-compose.yml`](./docker-compose.yml) lists them inline under each
service's `environment:` block — there is no `.env` file. Names are
case-insensitive. Values shown under **Default** are what PriceOrbit uses when
the variable is unset.

A ✓ in **Required** means you should set it for any real deployment; the rest are
optional.

## Application

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `APP_SECRET` | Secret key used to sign login session cookies. Changing it invalidates all existing sessions. | `change-me` | Any long, random string (32+ chars recommended). | ✓ |
| `APP_NAME` | Display name shown in the UI title and headings. | `PriceOrbit` | Any string. | |
| `TIMEZONE` | Time zone used for schedules and for displaying timestamps. | `UTC` | Any IANA time-zone name, e.g. `UTC`, `America/New_York`, `Europe/London`, `Asia/Tokyo`. | |
| `UPLOADS_DIR` | Directory inside the container where uploaded login-page assets (logo, background) are stored. Keep it on a mounted volume to persist. | `/data/uploads` | An absolute path inside the container. | |
| `APP_VERSION` | Informational version string shown on the Settings page. Not normally set by hand. | `0.5.0` | Any string. | |
| `LOGIN_TYPE` | **Sign-in override / lockout recovery.** When set, forces the login mode regardless of the Admin-UI setting, so a misconfigured OIDC can't lock you out — set it and redeploy to recover, then unset to manage sign-in from the UI again. `OFF` = no sign-in; `Standard` = local username/password only (OIDC off); `OIDC` = OIDC on (local login kept as a fallback). | (empty = use UI settings) | `OFF`, `Standard`, `OIDC` (case-insensitive). | |

## Logging

Both the web and worker processes log to stdout (`docker logs`) and to a shared
file, which the **Admin → Logs** page tails. The level is also editable at
runtime there (it persists to the database and overrides `LOG_LEVEL`); a runtime
change applies to the web app immediately and to the worker within ~5 minutes.

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `LOG_LEVEL` | Initial log level. Lower levels log more (DEBUG/TRACE include each scrape attempt). | `info` | `fatal`, `error`, `warn`, `info`, `debug`, `trace`. | |
| `LOG_FILE` | Path the combined log is written to (rotates at ~2 MB). Put it on a mounted volume to keep logs across restarts. | `/data/app.log` | An absolute path inside the container. | |

## Database

The application connects with the `DB_*` variables; the bundled MariaDB container
is configured with matching `MARIADB_DATABASE` / `MARIADB_USER` /
`MARIADB_PASSWORD` values, so the two sets must agree.

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `DB_PASSWORD` | Password for the application database user. | `priceorbit` | Any strong password. | ✓ |
| `DB_DRIVER` | SQLAlchemy database driver. | `mysql+pymysql` | `mysql+pymysql` (works for both MySQL and MariaDB). | |
| `DB_HOST` | Database hostname. `db` is the bundled container's service name. | `db` | Hostname or IP address. | |
| `DB_PORT` | Database port. | `3306` | Any valid TCP port. | |
| `DB_NAME` | Database/schema name. | `priceorbit` | Any valid database name. | |
| `DB_USER` | Database username. | `priceorbit` | Any valid username. | |

## Container / Compose

These are read by the bundled MariaDB container rather than by the application
itself. The `app` container applies database migrations automatically on every
start (from its entrypoint) — there is no migration toggle.

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `MARIADB_ROOT_PASSWORD` | Root password for the bundled MariaDB container. | (none) | Any strong password. | ✓ |
| `MARIADB_DATABASE` | Database created on first start. Must match `DB_NAME`. | (none) | Any valid database name. | ✓ |
| `MARIADB_USER` | Application database user created on first start. Must match `DB_USER`. | (none) | Any valid username. | ✓ |
| `MARIADB_PASSWORD` | Password for `MARIADB_USER`. Must match `DB_PASSWORD`. | (none) | Any strong password. | ✓ |

The host port is set directly in the `app` service's `ports:` mapping
(`8800:8000` in the bundled compose — host `8800`, container `8000`). Change the
left side to publish a different host port.

## Worker & scheduling

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `DEFAULT_CHECK_INTERVAL_MINUTES` | Check interval used for a product that has no explicit schedule. | `60` | Positive integer (minutes). | |
| `MIN_CHECK_INTERVAL_MINUTES` | Hard floor applied to any schedule, to prevent hammering a store. | `1` | Integer ≥ 1 (minutes). | |
| `SCHEDULER_RECONCILE_MINUTES` | How often the worker re-syncs its jobs with the database (new/changed/removed products). | `2` | Positive integer (minutes). | |
| `CHECK_JITTER_SECONDS` | Random spread added to scheduled check times so checks don't all fire at once. | `30` | Integer ≥ 0 (seconds). | |

## Scraping & politeness

Pages are fetched with engines tried in order: `curl_cffi` (impersonates a real
browser's TLS/HTTP2 fingerprint, clearing most fingerprint-based anti-bot), then
plain `httpx`. If a [scrape.do](https://scrape.do) token is configured, it's
appended as a **last-resort engine** — used only when both free engines are
blocked, so credits are spent only on heavy anti-bot stores (e.g. Akamai /
Home Depot). With no token, those stores simply can't be read.

> **scrape.do requires a free account.** Sign up at <https://scrape.do> (no card)
> and copy your API token. The free tier is **1,000 credits/month**, and a
> protected fetch (residential `super` + `render`, the default) costs **25
> credits** — so roughly **40 protected-store fetches/month** for free. Enough
> for a few anti-bot-protected products checked ~daily; heavier use needs a paid
> plan. See the README's "Scraping anti-bot-protected stores" section for the
> full rundown.

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `USER_AGENT` | HTTP `User-Agent` sent by the httpx fallback engine. | a current Chrome desktop UA | Any string. | |
| `IMPERSONATE_PROFILE` | Browser profile `curl_cffi` impersonates (TLS/JA3 + HTTP2 fingerprint). | `chrome` | `curl_cffi` profile, e.g. `chrome`, `chrome124`, `safari`. | |
| `RESPECT_ROBOTS` | Honour `robots.txt`. Off by default: this is a self-hosted monitor fetching user-chosen product URLs, and many stores' robots.txt would block the exact pages you want to track. The per-domain rate limit still applies. | `false` | `true` or `false`. | |
| `FETCH_TIMEOUT_SECONDS` | Per-request fetch timeout (free engines). | `15.0` | Positive number (seconds). | |
| `PER_DOMAIN_MIN_SECONDS` | Minimum gap between two requests to the same host (background checks only; the interactive "Add product" import skips it). | `20` | Integer ≥ 0 (seconds). | |
| `FETCH_JITTER_SECONDS` | Random delay added before each background fetch, on top of the per-domain gap. | `20` | Integer ≥ 0 (seconds). | |
| `SCRAPEDO_TOKEN` | scrape.do API token. Empty = the paid engine is disabled. | (empty) | Your scrape.do token. | |
| `SCRAPEDO_RENDER` | Run JS in scrape.do's headless browser. | `true` | `true` or `false`. | |
| `SCRAPEDO_SUPER` | Use scrape.do residential/mobile proxies (needed for Akamai). Costs more credits. | `true` | `true` or `false`. | |
| `SCRAPEDO_GEO` | scrape.do `geoCode` (proxy country). | `US` | ISO country code, or blank. | |
| `SCRAPEDO_TIMEOUT_SECONDS` | Timeout for scrape.do requests (render + residential is slow). | `70.0` | Positive number (seconds). | |
| `SCRAPEDO_MONTHLY_CREDITS` | Assumed monthly credit allowance, used for the usage meter on the Settings page. scrape.do's `/info` API only reports real numbers for paid plans, so free-tier usage is tracked locally against this. | `1000` | Positive integer. | |

## Notifications (optional)

Email and Telegram are usually configured in the **Alerts** page after launch
(those values are stored encrypted in the database). The variables below let you
provide the same settings at deploy time instead. Leaving `SMTP_HOST` /
`TELEGRAM_BOT_TOKEN` empty simply disables that channel.

| Variable | Description | Default | Possible values | Required |
| --- | --- | --- | --- | --- |
| `SMTP_HOST` | SMTP server hostname for email alerts. Empty disables email. | (empty) | Hostname, e.g. `smtp.gmail.com`. | |
| `SMTP_PORT` | SMTP server port. | `587` | `587` (STARTTLS), `465` (implicit TLS), `25` (plain). | |
| `SMTP_USER` | SMTP username. | (empty) | Any string. | |
| `SMTP_PASSWORD` | SMTP password or app password. | (empty) | Any string. | |
| `SMTP_FROM` | "From" address used on outgoing email. | (empty) | An email address. | |
| `SMTP_USE_TLS` | Use STARTTLS for the SMTP connection. | `true` | `true` or `false`. | |
| `TELEGRAM_BOT_TOKEN` | Bot token (from BotFather) for Telegram alerts. Empty disables Telegram. | (empty) | A token string like `123456:ABC-DEF...`. | |

## Setting these in compose

Values go directly under each service's `environment:` block — no `.env` file.
Minimal example of the secrets to change (everything else has a sensible
default):

```yaml
  db:
    environment:
      MARIADB_DATABASE: priceorbit
      MARIADB_USER: priceorbit
      MARIADB_PASSWORD: a-strong-database-password
      MARIADB_ROOT_PASSWORD: a-different-strong-root-password

  app:
    environment:
      APP_SECRET: please-change-this-to-a-long-random-value
      DB_NAME: priceorbit
      DB_USER: priceorbit
      DB_PASSWORD: a-strong-database-password   # matches MARIADB_PASSWORD
      TIMEZONE: America/New_York
      # Email alerts (optional — can also be set in the Alerts page)
      SMTP_HOST: smtp.example.com
      SMTP_PORT: "587"
      SMTP_USER: alerts@example.com
      SMTP_PASSWORD: app-password-here
      SMTP_FROM: alerts@example.com
```
