# intervals.icu MCP Server

An [MCP](https://modelcontextprotocol.io) server that makes a user's
[intervals.icu](https://intervals.icu) training data available to Claude.

Authentication runs through **Keycloak** (OAuth 2.1). Each Keycloak user carries
their intervals.icu **Athlete ID** and **API Key** as user attributes; a protocol
mapper puts both into the access token as claims, and the MCP server reads them to
call intervals.icu on the user's behalf.

> Design rationale and alternatives are in [`docs/KONZEPT.md`](docs/KONZEPT.md).
> This repo implements **Variant 1**: both credentials travel inside the JWT.

## Tools

| Tool | Description |
|---|---|
| `list_activities(oldest, newest)` | Activities between two ISO dates |
| `get_activity(activity_id)` | Full detail for one activity |
| `get_activity_streams(activity_id, block_seconds=60, types?)` | Detailed per-sample time-series (power, HR, speed, cadence, altitude, GPS, …) aggregated into fixed-length time blocks, with derived headwind and watts-per-heartbeat and activity-level weather |
| `get_activity_intervals(activity_id)` | Detected intervals / laps analysis (per-effort power, HR, cadence, …) |
| `get_wellness(oldest, newest)` | Daily wellness / recovery records |
| `list_events(oldest, newest)` | Calendar events / planned workouts |
| `get_athlete()` | Profile, FTP and training zones |

The athlete is fixed by the login — tools never accept an athlete id, so a user
can only ever reach their own data.

## Architecture

```
Claude ──OAuth 2.1 (PKCE)──► Keycloak ──JWT (athlete_id, api_key claims)──►
Claude ──Bearer JWT──► MCP server ──Basic auth (API_KEY:key)──► intervals.icu
```

The MCP server is an OAuth **resource server**:
- publishes `/.well-known/oauth-protected-resource` pointing to the Keycloak realm,
- validates each bearer JWT against Keycloak's JWKS (signature, issuer, audience, expiry),
- extracts the two claims and calls intervals.icu.

## Deployment

### Production — MCP container only (`docker-compose.yml`)

Use this when **Keycloak already exists** in your infrastructure and TLS is
terminated by your own reverse proxy.

```bash
cp .env.example .env     # set OIDC_ISSUER and MCP_RESOURCE_URL
docker compose up -d --build
```

The container serves plain HTTP on `${MCP_PORT:-8000}` at path `/mcp`. Front it
with your TLS-terminating proxy and expose it at `MCP_RESOURCE_URL`.

A prebuilt image is published to GHCR on every push to `main` by the
[`docker-publish`](.github/workflows/docker-publish.yml) workflow. To use it
instead of building locally, set in `docker-compose.yml`:

```yaml
    image: ghcr.io/alexlambertz/intervalsicu-mcp:latest
```

Required environment:

| Variable | Example | Meaning |
|---|---|---|
| `OIDC_ISSUER` | `https://auth.example.com/realms/intervals` | Your Keycloak realm issuer |
| `MCP_RESOURCE_URL` | `https://mcp.example.com` | Public URL of this server |
| `MCP_AUDIENCE` | `intervals-mcp-server` | Must match the Keycloak audience mapper |
| `ATHLETE_CLAIM` / `APIKEY_CLAIM` | `intervals_athlete_id` / `intervals_api_key` | Claim names |
| `INTERVALS_BASE_URL` | `https://intervals.icu/api/v1` | intervals.icu API base |

### Local / all-in-one (`docker-compose.dev.yml`)

Brings up Keycloak + Postgres + Caddy + the MCP server, and imports the realm
from `keycloak/realm-export.json`.

```bash
cp .env.example .env     # set AUTH_DOMAIN, MCP_DOMAIN, KC_* passwords
docker compose -f docker-compose.dev.yml up -d --build
```

## Keycloak setup

`keycloak/realm-export.json` provisions a realm `intervals` with:
- a public PKCE client `claude-mcp`,
- a declarative user profile declaring `intervals_athlete_id` and `intervals_api_key`,
- protocol mappers writing both attributes into the access token,
- an audience mapper adding `intervals-mcp-server`,
- a demo user (set a real API key before use).

To onboard a user: set their `intervals_athlete_id` (e.g. `i123456`) and
`intervals_api_key` attributes in the Keycloak admin console (Users → *user* →
Attributes). The API key comes from intervals.icu → Settings → Developer.

## Releasing

Two workflows handle CI/CD:

- [`docker-publish`](.github/workflows/docker-publish.yml) — on every push to
  `main`, builds and pushes `ghcr.io/<owner>/intervalsicu-mcp` tagged `latest`
  and with the short commit SHA.
- [`release`](.github/workflows/release.yml) — on a pushed version tag, builds
  the image, tags it `latest` and with the version number, and creates a GitHub
  Release with auto-generated notes.

Cut a release by pushing a semver tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

This publishes `…:1.0.0`, `…:1.0`, `…:1`, and `…:latest`, and opens a GitHub
Release for `v1.0.0`.

## Development

```bash
cd server
python -m venv .venv && . .venv/bin/activate
pip install -e .
# requires the resource-server env vars (OIDC_ISSUER, MCP_RESOURCE_URL, ...)
python -m intervals_mcp
```
