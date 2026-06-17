# PriceOrbit

PriceOrbit is a lightweight, self-hosted price and stock monitor. Point it at
product pages from different stores and it tracks each listing's price and
availability on a schedule, keeps a price history with a chart, and alerts you
when a price drops below a target, falls by an amount or percentage, or when an
item comes back in stock. Alerts can be delivered by email, Telegram, or an
in-browser sound.

It runs as two containers: the **app** (a single container that serves the UI
and API and runs the background scheduler), and a **MariaDB database**.

## Features

- Track price and stock across multiple stores per product, with per-product or
  per-store check schedules.
- Price-history chart, statistics, and at-a-glance deal / record-low indicators.
- Alert rules — drop by amount, drop by percent, at/under a target, or back in
  stock — with per-rule cooldowns and global quiet hours.
- Notifications by email, Telegram, or browser sound, plus a filterable
  notification log.
- Optional multi-user mode with local accounts and OIDC single sign-on.
- Admin area (admin-only): database status, security &amp; sign-in, login-page
  customization, SSO config, and a logs/debug page with a configurable level
  (fatal · error · warn · info · debug · trace).
- Monitoring health: per-product status, a "check all now" action, and
  home-page summary tiles.

## Deploy with Docker Compose

A prebuilt image is published at `ghcr.io/fluxden/priceorbit:latest`. The single
`app` container starts both the web server and the scheduler worker itself
(via the image entrypoint) and applies database migrations on startup — there
are no `command:` overrides to set. All configuration is passed as environment
variables listed directly in the compose file (no `.env` file).

The provided [`docker-compose.yml`](./docker-compose.yml) attaches both services
to an external Docker network named `proxy`, intended for a reverse proxy in a
Docker Stack on a bridged network. Create it once before deploying:

```bash
docker network create proxy
```

**1. Use this `docker-compose.yml`:**

```yaml
services:
  db:
    image: mariadb:11
    restart: unless-stopped
    environment:
      MARIADB_DATABASE: priceorbit
      MARIADB_USER: priceorbit
      MARIADB_PASSWORD: CHANGE_ME
      MARIADB_ROOT_PASSWORD: CHANGE_ME
    volumes:
      - db_data:/var/lib/mysql
    networks:
      - proxy
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect"]
      interval: 10s
      timeout: 5s
      retries: 5

  app:
    image: ghcr.io/fluxden/priceorbit:latest
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      APP_NAME: PriceOrbit
      APP_SECRET: CHANGE_ME
      TIMEZONE: UTC
      DB_DRIVER: mysql+pymysql
      DB_HOST: db
      DB_PORT: "3306"
      DB_NAME: priceorbit
      DB_USER: priceorbit
      DB_PASSWORD: CHANGE_ME
    ports:
      - "8800:8000"
    networks:
      - proxy
    volumes:
      - uploads_data:/data/uploads

volumes:
  db_data:
  uploads_data:

networks:
  proxy:
    external: true
```

**2. Set the secrets.** Replace every `CHANGE_ME` before deploying:

- `APP_SECRET` — a long random string.
- `DB_PASSWORD` — must match in **both** the `db` and `app` services.
- `MARIADB_PASSWORD` — same value as `DB_PASSWORD`.
- `MARIADB_ROOT_PASSWORD` — a different strong value.

**3. Start it:**

```bash
docker compose up -d
```

Open `http://<your-host>:8800`. On first start the `app` container creates the
database schema automatically (migrations run from the entrypoint). The host
port is `8800`; the app listens on `8000` inside the container — change only the
left side of `8800:8000` to publish a different host port.

**To update:** `docker compose pull && docker compose up -d`.

## Required environment variables

The compose file lists every variable inline. The ones you must change from
their `CHANGE_ME` placeholders for a secure deployment:

| Variable | Why it is required |
| --- | --- |
| `APP_SECRET` | Secret key that signs login session cookies. Use a long random string. Changing it later signs everyone out. |
| `DB_PASSWORD` / `MARIADB_PASSWORD` | Password for the application's database user. The `app` and `db` services must both see the same value. |
| `MARIADB_ROOT_PASSWORD` | Root password for the bundled MariaDB container. |

`DB_NAME`, `DB_USER`, `DB_HOST`, and `DB_PORT` default to `priceorbit`,
`priceorbit`, `db`, and `3306`; change them only when pointing at an external
database.

A complete list of every variable, its default, and accepted values is in
[`ENVIRONMENT.md`](./ENVIRONMENT.md).

## Using an external database

Set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` on the `app`
service to point at your server, then remove the bundled `db` service (and its
`depends_on` entry). MySQL 8 and MariaDB 11 are both supported with the default
`DB_DRIVER=mysql+pymysql`.

## Scraping anti-bot-protected stores (optional)

Most stores work out of the box. Product pages are fetched with a
browser-impersonating client (`curl_cffi`, real TLS/HTTP2 fingerprint) and a
plain `httpx` fallback, which together read most static and server-rendered
stores. A few large retailers (notably **Home Depot**) sit behind enterprise
anti-bot (Akamai) that blocks *every* server-side request — even a real
self-hosted headless browser. To read those, PriceOrbit can fall back to
[scrape.do](https://scrape.do), a scraping API that routes the request through
residential proxies plus a headless browser.

This is **optional and disabled by default**. With no token set, nothing
changes (and Akamai-protected stores simply fail with a clear message).

**Setup — you must create a free scrape.do account:**

1. Sign up at **<https://scrape.do>** — free, no credit card required.
2. Copy your API **token** from the dashboard.
3. Add it to the `app` service in `docker-compose.yml` (uncomment the line):
   ```yaml
       SCRAPEDO_TOKEN: your-token-here
   ```
   then `docker compose up -d`.

scrape.do is tried **only as a last resort** — after the free built-in engines
are blocked — so credits are spent solely on the hard stores. Normal stores
never touch it.

**Free-tier limitations (important):**

- **1,000 credits per month**, renewing monthly; 5 concurrent requests. Only
  *successful* requests are charged.
- Anti-bot stores require residential proxies **and** JS rendering, which
  PriceOrbit enables by default (`SCRAPEDO_SUPER` + `SCRAPEDO_RENDER`). That
  combination costs **25 credits per request** (residential 10× × rendering
  5× ... = 25×) → roughly **40 protected-store fetches per month** on the free
  plan.
- Practical meaning: free is fine for **a handful of Akamai-protected products
  checked about once a day**. Many protected products, or frequent checks, will
  exhaust the credits — after which those stores fail until the monthly reset (or
  you move to a [paid plan](https://scrape.do/pricing/), from ~$29/mo).
- scrape.do is **slower** (residential + render = several seconds up to ~a minute
  per fetch) and **not guaranteed** for every site or forever — anti-bot is an
  arms race.
- To stretch credits, you can disable residential (`SCRAPEDO_SUPER: "false"`,
  drops to 5 credits/request) — but that won't get past Akamai. See
  [`ENVIRONMENT.md`](./ENVIRONMENT.md) for all `SCRAPEDO_*` options.

Set a sensible check interval for protected products (e.g. daily) so you don't
burn through the monthly free credits.

## Notes

- Email and Telegram credentials are normally configured in the **Alerts** page
  after first launch; the matching environment variables (see `ENVIRONMENT.md`)
  can be used instead if you prefer to set them at deploy time.
- The browser-sound alert needs the tab open and one prior interaction with the
  page (standard browser autoplay rules).
- **Locked out by sign-in/OIDC?** Set `LOGIN_TYPE` on the `app` service and
  redeploy — it overrides the stored settings so you can always get back in:
  `OFF` (no sign-in), `Standard` (local password only, OIDC off), or `OIDC`.
  While set, the Admin sign-in/OIDC controls are disabled with a notice; unset it
  to manage from the UI again. See [`ENVIRONMENT.md`](./ENVIRONMENT.md).
- Log verbosity is set with `LOG_LEVEL` (`fatal`→`trace`) or live in **Admin →
  Logs**; both web and worker write to `LOG_FILE` (default `/data/app.log`).
