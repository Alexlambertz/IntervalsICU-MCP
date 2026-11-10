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

### Write tools

60 tools cover the intervals.icu write API (generated against the official OpenAPI
spec). Bodies are free-form objects, because the intervals.icu schemas are huge
(Activity: 184 fields, Athlete: 164) — call **`get_write_schema(name, search?)`** to
look up valid field names, types and enum values (e.g. `get_write_schema("Wellness",
"sleep")`).

| Area | Tools |
|---|---|
| **Activities** | `update_activity`, `delete_activity`, `delete_activity_tombstone`, `create_manual_activity`, `create_manual_activities_bulk`, `upload_activity_file`¹, `replace_activity_file`¹, `add_activity_comment`, `update_activity_streams`, `update_activity_streams_csv`, `update_activity_intervals`, `update_activity_interval`, `delete_activity_intervals`, `split_activity_interval` |
| **Calendar events** | `create_event`, `create_events_bulk`, `update_event`, `update_events_in_range`, `delete_event`, `delete_events_in_range`, `delete_events_bulk`, `mark_event_done`, `duplicate_events`, `apply_plan`, `apply_plan_changes` |
| **Wellness** | `update_wellness`, `update_wellness_bulk`, `upload_wellness_csv` |
| **Workout library** | `create_workout`, `create_workouts_bulk`, `update_workout`, `delete_workout`, `update_plan_workouts`, `duplicate_workouts`, `import_workout_file`¹ |
| **Folders / plans** | `create_folder`, `update_folder`, `delete_folder`, `update_folder_shared_with`, `share_folder_with_me` |
| **Gear** | `create_gear`, `update_gear`, `delete_gear`, `replace_gear`, `create_gear_reminder`, `update_gear_reminder`, `delete_gear_reminder` |
| **Athlete / settings** | `update_athlete`, `create_sport_settings`, `update_sport_settings`, `update_sport_settings_multi`, `delete_sport_settings`, `update_weather_config`, `update_training_plan`, `update_route` |
| **Custom items** | `create_custom_item`, `update_custom_item`, `delete_custom_item`, `reorder_custom_items`, `upload_custom_item_image`¹ |

¹ File content is passed base64-encoded (`content_base64`); limit `MAX_UPLOAD_BYTES`
(default 3 MiB, the MCP request body is capped at 4 MiB).

**Safety**
- Every write tool carries MCP annotations (`readOnlyHint=false`, and
  `destructiveHint=true` for deletes and range operations), so MCP clients can ask
  for confirmation. Delete tools say so in their description.
- Range operations (`delete_events_in_range`, `update_events_in_range`) require both
  dates — the API's default "everything in the future" is deliberately not exposed.
- IDs are validated as single URL path segments (no `../` redirection to other
  endpoints), dates as ISO dates, uploads as valid base64 within the size limit —
  all before any request is sent.
- Set **`MCP_READ_ONLY=true`** to run a read-only deployment: no write tool is
  registered at all, so the model cannot change data even if asked to.

**Deliberately not exposed:** chats/messaging (`/chats/*`, would message other
people), `/athlete-plans` (multi-athlete coach operation), `/disconnect-app` (would
revoke the access this server uses), the `download-workout*` / `download-fit-files`
conversions (downloads, not writes) and the duplicate `PUT /athlete/{id}/wellness`.

The field reference lives in `server/intervals_mcp/write_schemas.json` and is
regenerated from the live spec with `python scripts/gen_write_schemas.py`.

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
| `MCP_SCOPES` | `openid` | OAuth scopes advertised to clients |
| `MCP_READ_ONLY` | `false` | `true` = register no write/delete tools |
| `MAX_UPLOAD_BYTES` | `3145728` | Max decoded size of file uploads |

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
