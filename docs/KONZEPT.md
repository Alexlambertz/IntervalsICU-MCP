# Konzept: intervals.icu MCP-Server mit Keycloak-Authentifizierung

> Status: Umgesetzt (Variante 1) · Stand: 2026-10-31
> Ziel: Einen MCP-Server bereitstellen, der die intervals.icu-API für Claude (und andere MCP-Clients) zugänglich macht. Die Authentifizierung läuft über einen eigenen Keycloak-Server; **Athlete ID** und **API Key** werden jeweils als Keycloak-Benutzerattribut hinterlegt und an den MCP-Server übergeben. Betrieb als Docker-Container via `docker-compose`.

---

## 1. Überblick & Ziele

Claude soll auf die Trainingsdaten eines Nutzers bei [intervals.icu](https://intervals.icu) zugreifen können (Aktivitäten, Wellness, geplante Workouts etc.), ohne dass jeder Nutzer manuell API-Keys in Claude eintragen muss. Stattdessen:

1. Der Nutzer meldet sich **einmalig** per OAuth 2.1 bei **Keycloak** an (Login im Browser, den Claude öffnet).
2. Keycloak kennt pro Nutzer dessen **intervals.icu Athlete ID** und **API Key** (als Benutzerattribute hinterlegt).
3. Der MCP-Server erhält diese Credentials sicher über das Token bzw. Keycloak und ruft damit die intervals.icu-API im Namen des Nutzers auf.

### Nicht-Ziele (für die erste Ausbaustufe)
- Kein Self-Service-Portal, in dem Nutzer ihren API-Key selbst eintragen (Attribute werden zunächst administrativ gepflegt — siehe [Abschnitt 9](#9-erweiterungen--roadmap)).
- Kein Caching/Persistieren von intervals.icu-Daten (der Server ist ein reiner, zustandsloser Proxy).

---

## 2. Grundlagen

### 2.1 intervals.icu API
- **Base URL:** `https://intervals.icu/api/v1`
- **Authentifizierung:** HTTP Basic Auth
  - Username: literal `API_KEY`
  - Password: der persönliche API-Key des Nutzers (aus intervals.icu → Settings → Developer)
- **Athlete-scoped Endpunkte** benötigen die Athlete ID im Pfad, z. B.:
  - `GET /api/v1/athlete/{id}/activities?oldest=…&newest=…`
  - `GET /api/v1/athlete/{id}/activities/{activityId}`
  - `GET /api/v1/athlete/{id}/wellness?oldest=…&newest=…`
  - `GET /api/v1/athlete/{id}/events?oldest=…&newest=…` (geplante Workouts/Kalender)
- Für den Zugriff braucht der Server also **beides**: Athlete ID (Pfad) + API Key (Auth). Genau diese beiden Werte liefert Keycloak.

### 2.2 MCP-Authorization (OAuth 2.1)
Der MCP-Standard definiert ein Authorization-Modell auf Basis von **OAuth 2.1**:
- Der **MCP-Server** agiert als **OAuth Resource Server**: Er akzeptiert nur Requests mit gültigem `Authorization: Bearer <access_token>`.
- **Keycloak** agiert als **Authorization Server**: Er authentifiziert den Nutzer und stellt Access Tokens (JWT) aus.
- Der **MCP-Client** (Claude) führt den Authorization-Code-Flow mit **PKCE** durch.
- Discovery läuft über Metadata-Endpunkte:
  - MCP-Server veröffentlicht **Protected Resource Metadata** (RFC 9728) unter `/.well-known/oauth-protected-resource` und verweist darauf, welcher Authorization Server zuständig ist (Keycloak-Realm).
  - Keycloak veröffentlicht **Authorization Server Metadata** (RFC 8414) unter `/.well-known/openid-configuration` bzw. `/.well-known/oauth-authorization-server` des Realms.

> **Transport:** Da ein Browser-OAuth-Flow und zentrales Hosting nötig sind, wird der **Streamable-HTTP-Transport** (bzw. HTTP+SSE) verwendet — **nicht** stdio. Der Server ist also über HTTPS erreichbar.

---

## 3. Architektur

```
                        ┌──────────────────────────────────────────────┐
                        │                 Claude (MCP-Client)            │
                        └───────────────┬───────────────▲────────────────┘
                                        │               │
                      (1) OAuth-Login   │               │ (4) MCP-Calls mit
                          im Browser    │               │     Bearer-Token
                                        ▼               │
   ┌───────────────────────┐   ┌────────────────────────┴───────────────┐
   │       Keycloak         │   │            MCP-Server                   │
   │  (Authorization Server)│◄──┤  (Resource Server, FastMCP/HTTP)        │
   │                        │   │                                         │
   │  Realm: intervals      │   │  - prüft JWT (Signatur via JWKS,        │
   │  Client: claude-mcp     │   │    issuer, audience, exp)              │
   │  User-Attribute:        │  │  - liest athlete_id + api_key          │
   │   • intervals_athlete_id│  │    aus Token-Claims (oder Keycloak)     │
   │   • intervals_api_key   │  │  - ruft intervals.icu API auf           │
   │  Protocol Mapper ───────┼──┘                                         │
   └──────────┬─────────────┘   └───────────────┬─────────────────────────┘
              │                                 │
              │ (2) Token mit Custom Claims     │ (5) Basic Auth
              │     (athlete_id, api_key)       │     (API_KEY:apikey)
              ▼                                 ▼
        ┌──────────┐                   ┌─────────────────────┐
        │ Postgres │                   │   intervals.icu API  │
        │ (KC-DB)  │                   │  api/v1/athlete/{id} │
        └──────────┘                   └─────────────────────┘
```

**Komponenten:**
| Komponente | Rolle | Technologie |
|---|---|---|
| `mcp-server` | MCP Resource Server + intervals.icu-Proxy | Python 3.12 + MCP SDK (FastMCP), `httpx`, `PyJWT`/`authlib` |
| `keycloak` | OAuth 2.1 Authorization Server / OIDC Provider | Keycloak (quay.io/keycloak/keycloak) |
| `keycloak-db` | Persistenz für Keycloak | PostgreSQL |
| `reverse-proxy` | TLS-Terminierung, Routing | Caddy oder Traefik |

---

## 4. Authentifizierungs-Flow (Detail)

```
Claude                 MCP-Server                 Keycloak                intervals.icu
  │  initial request       │                          │                        │
  ├───────────────────────►│                          │                        │
  │  401 + WWW-Authenticate │                          │                        │
  │  (resource_metadata)    │                          │                        │
  │◄───────────────────────┤                          │                        │
  │  GET /.well-known/oauth-protected-resource         │                        │
  ├───────────────────────►│                          │                        │
  │  { authorization_servers: [keycloak-realm] }       │                        │
  │◄───────────────────────┤                          │                        │
  │  OIDC-Discovery + (Dynamic Client Registration)    │                        │
  ├────────────────────────┼─────────────────────────►│                        │
  │  Authorization Code Flow + PKCE (Browser-Login)    │                        │
  ├────────────────────────┼─────────────────────────►│                        │
  │  access_token (JWT) mit Claims:                    │                        │
  │   intervals_athlete_id, intervals_api_key          │                        │
  │◄───────────────────────┼──────────────────────────┤                        │
  │  MCP-Call  Authorization: Bearer <JWT>             │                        │
  ├───────────────────────►│                          │                        │
  │                        │ JWT-Validierung (JWKS)    │                        │
  │                        ├─────────────────────────►│ (JWKS-Abruf, gecacht)  │
  │                        │ Basic Auth mit api_key    │                        │
  │                        ├──────────────────────────┼───────────────────────►│
  │                        │ Daten                     │                        │
  │                        │◄─────────────────────────┼────────────────────────┤
  │  Tool-Ergebnis          │                          │                        │
  │◄───────────────────────┤                          │                        │
```

**Schritte:**
1. Claude ruft den MCP-Server ohne Token auf → `401` mit `WWW-Authenticate`-Header, der auf die Protected-Resource-Metadata verweist.
2. Claude entdeckt Keycloak als Authorization Server, registriert sich (Dynamic Client Registration, falls aktiviert, sonst vorkonfigurierter public Client) und startet den Authorization-Code-Flow mit PKCE.
3. Der Nutzer meldet sich bei Keycloak im Browser an. Keycloak stellt ein JWT-Access-Token aus. Über **Protocol Mapper** werden die Benutzerattribute `intervals_athlete_id` und `intervals_api_key` als Claims in das Token geschrieben.
4. Claude ruft MCP-Tools mit `Authorization: Bearer <JWT>` auf.
5. Der MCP-Server validiert das JWT (Signatur gegen Keycloak-JWKS, `iss`, `aud`, `exp`), extrahiert Athlete ID + API Key und ruft damit intervals.icu auf.

---

## 5. Keycloak-Konfiguration

### 5.1 Realm & Client
- **Realm:** `intervals`
- **Client:** `claude-mcp`
  - Client-Typ: `public` (PKCE erzwungen) — passend für MCP-Clients ohne Client-Secret.
  - Standard-Flow: Authorization Code + PKCE.
  - Gültige Redirect-URIs: die von Claude genutzten Callback-URLs (z. B. `https://claude.ai/api/mcp/auth_callback` und für lokale Clients `http://localhost:*`). Die exakten URIs ergeben sich aus dem verwendeten Claude-Client; ggf. per Wildcard für localhost während der Entwicklung.
  - **Audience:** Das Token muss den MCP-Server als `aud` enthalten (Audience-Mapper, siehe unten) — wichtig, damit der Resource Server das Token akzeptiert und Token-Passthrough verhindert wird.
- Optional: **Dynamic Client Registration** aktivieren (Client Registration Policy), damit sich MCP-Clients selbst registrieren können. Alternativ ein statischer public Client.

### 5.2 Benutzerattribute
Pro Keycloak-User werden zwei Attribute gepflegt (Admin-Konsole → Users → *User* → Attributes, bzw. über User Profile Schema):

| Attribut | Beispielwert | Beschreibung |
|---|---|---|
| `intervals_athlete_id` | `i123456` | Die Athlete ID des Nutzers bei intervals.icu |
| `intervals_api_key` | `abcd1234efgh5678` | Der persönliche API-Key des Nutzers |

> In neueren Keycloak-Versionen muss das **User Profile** (Declarative User Profile) um diese Attribute erweitert werden, damit sie gespeichert und gemappt werden dürfen.

### 5.3 Protocol Mapper (Attribute → Token-Claims)
Für den Client `claude-mcp` (oder über einen Client Scope) je ein **User Attribute Mapper**:

| Mapper | Einstellung |
|---|---|
| Athlete-ID-Mapper | Type: *User Attribute* · User Attribute: `intervals_athlete_id` · Token Claim Name: `intervals_athlete_id` · Add to access token: ✅ · Claim JSON Type: String |
| API-Key-Mapper | Type: *User Attribute* · User Attribute: `intervals_api_key` · Token Claim Name: `intervals_api_key` · Add to access token: ✅ · Claim JSON Type: String |
| Audience-Mapper | Type: *Audience* · Included Custom Audience: `intervals-mcp-server` (die Resource-URI/der Audience-Wert des MCP-Servers) |

Damit enthält das Access Token (JWT) nach dem Login u. a.:
```json
{
  "iss": "https://auth.example.com/realms/intervals",
  "aud": "intervals-mcp-server",
  "sub": "f3a1…",
  "exp": 1730400000,
  "intervals_athlete_id": "i123456",
  "intervals_api_key": "abcd1234efgh5678"
}
```

> ✅ **Gewählte Variante (Variante 1 / umgesetzt):** Beide Werte liegen als Claims direkt im Access Token. Das ist der direkte Weg der gewünschten Attribut-Übergabe und in dieser Codebasis implementiert. Bewusst in Kauf genommen: der API-Key (ein Geheimnis) wird in jedem Request mitgeschickt und kann theoretisch in Logs landen — daher kurze Token-Lebensdauer, kein Logging von Tokens und HTTPS-only (siehe [Abschnitt 8](#8-sicherheitsüberlegungen)). Die in Abschnitt 8 beschriebene Alternative (serverseitiger Abruf über die Keycloak Admin-API) bleibt als spätere Option dokumentiert.

---

## 6. MCP-Server Design

### 6.1 Stack
- **Sprache:** Python 3.12
- **Framework:** MCP Python SDK (`mcp`, FastMCP) mit Streamable-HTTP-Transport
- **HTTP-Client:** `httpx` (async) für intervals.icu
- **Token-Validierung:** `authlib` / `PyJWT` mit JWKS-Caching

### 6.2 Token-Validierung (Resource Server)
Für jeden eingehenden Request:
1. `Authorization: Bearer`-Header lesen → JWT.
2. Signatur gegen Keycloak-JWKS prüfen (JWKS-URL aus OIDC-Discovery, Keys gecacht, Rotation beachten).
3. Claims prüfen: `iss` == Keycloak-Realm, `aud` enthält den MCP-Server, `exp`/`nbf` gültig.
4. `intervals_athlete_id` (+ ggf. `intervals_api_key`) aus Claims lesen. Fehlt eins → `403` mit klarer Fehlermeldung (Attribut im Keycloak-Profil nicht gepflegt).
5. Pro-Request-Kontext mit diesen Credentials aufbauen; **keine** serverseitige Persistenz.

### 6.3 Bereitgestellte MCP-Tools (erste Ausbaustufe)
| Tool | Zweck | intervals.icu-Endpunkt |
|---|---|---|
| `list_activities` | Aktivitäten in einem Zeitraum auflisten | `GET /athlete/{id}/activities` |
| `get_activity` | Details/Streams einer Aktivität | `GET /athlete/{id}/activities/{actId}` |
| `get_wellness` | Wellness-/Recovery-Daten (HRV, Ruhepuls, Gewicht …) | `GET /athlete/{id}/wellness` |
| `list_events` | Geplante Workouts / Kalender | `GET /athlete/{id}/events` |
| `get_athlete` | Profil, FTP/Zonen | `GET /athlete/{id}` |

Die `athlete_id` kommt **immer** aus dem Token, nie als Tool-Parameter — so kann ein Nutzer nicht die Daten eines anderen Athleten anfragen.

### 6.4 Fehlerbehandlung
- `401` bei fehlendem/ungültigem Token (mit `WWW-Authenticate` + Metadata-Pointer).
- `403` bei gültigem Token, aber fehlenden intervals-Attributen.
- intervals.icu-Fehler (401/404/429) werden in sprechende MCP-Tool-Fehler übersetzt (z. B. „API-Key ungültig", „Rate Limit erreicht").

---

## 7. Deployment (docker-compose)

Es gibt zwei Compose-Dateien:
- **`docker-compose.yml` (Produktiv):** nur der MCP-Container. Keycloak und TLS/Reverse-Proxy stellt die vorhandene Infrastruktur. Benötigt `OIDC_ISSUER` (bestehender Keycloak-Realm) und `MCP_RESOURCE_URL`.
- **`docker-compose.dev.yml` (lokal / all-in-one):** zusätzlich Keycloak + Postgres + Caddy, importiert den Realm aus `keycloak/realm-export.json`.

### 7.1 Services (all-in-one, dev)
```yaml
services:
  reverse-proxy:
    image: caddy:2-alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on: [keycloak, mcp-server]

  keycloak-db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: keycloak
      POSTGRES_USER: keycloak
      POSTGRES_PASSWORD: ${KC_DB_PASSWORD}
    volumes:
      - kc_db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U keycloak"]
      interval: 10s
      retries: 5

  keycloak:
    image: quay.io/keycloak/keycloak:26.0
    command: start
    environment:
      KC_DB: postgres
      KC_DB_URL: jdbc:postgresql://keycloak-db:5432/keycloak
      KC_DB_USERNAME: keycloak
      KC_DB_PASSWORD: ${KC_DB_PASSWORD}
      KC_HOSTNAME: auth.example.com
      KC_PROXY_HEADERS: xforwarded
      KC_HTTP_ENABLED: "true"
      KC_BOOTSTRAP_ADMIN_USERNAME: ${KC_ADMIN}
      KC_BOOTSTRAP_ADMIN_PASSWORD: ${KC_ADMIN_PASSWORD}
    depends_on:
      keycloak-db:
        condition: service_healthy

  mcp-server:
    build: ./server
    environment:
      OIDC_ISSUER: https://auth.example.com/realms/intervals
      MCP_AUDIENCE: intervals-mcp-server
      INTERVALS_BASE_URL: https://intervals.icu/api/v1
      # Nur bei serverseitigem Attribut-Abruf (empfohlene Variante, Abschnitt 8):
      KC_ADMIN_CLIENT_ID: ${KC_SA_CLIENT_ID}
      KC_ADMIN_CLIENT_SECRET: ${KC_SA_CLIENT_SECRET}
    depends_on: [keycloak]

volumes:
  kc_db_data:
  caddy_data:
```

### 7.2 TLS / Reverse Proxy
- Caddy terminiert TLS (automatische Zertifikate via Let's Encrypt) und routet:
  - `auth.example.com` → `keycloak:8080`
  - `mcp.example.com` → `mcp-server:8000`
- OAuth und MCP erfordern HTTPS — in Produktion zwingend.

### 7.3 Secrets & Konfiguration
- Alle Secrets (`KC_DB_PASSWORD`, Admin-Credentials, Service-Account-Secret) via `.env` / Docker Secrets, **nicht** im Compose-File hartkodiert.
- Keycloak-Realm per **Realm-Import** (`--import-realm` + gemountete `realm-export.json`) reproduzierbar provisionieren, inklusive Client, Mapper und User-Profile-Schema.

---

## 8. Sicherheitsüberlegungen

| Thema | Maßnahme |
|---|---|
| **API-Key im Token** | Empfohlene Variante: nur `intervals_athlete_id` ins Token; den `intervals_api_key` holt der MCP-Server serverseitig über die **Keycloak Admin REST API** (`GET /admin/realms/intervals/users/{sub}`) mit einem eigenen Service-Account-Client (Rolle `view-users`). So verlässt das Geheimnis nie den Server-zu-Keycloak-Kanal und landet nicht in Client-/Proxy-Logs. (Der Nutzer-Wunsch „per Attribut übergeben" bleibt erfüllt.) |
| **Audience-Binding** | `aud`-Claim zwingend prüfen → verhindert, dass ein für einen anderen Dienst ausgestelltes Token hier akzeptiert wird (Token-Passthrough-Schutz, MCP-Vorgabe). |
| **Token-Lebensdauer** | Kurze Access-Token-TTL (z. B. 5–15 min) + Refresh Token; begrenzt Schaden bei Leak. |
| **Transport** | Ausschließlich HTTPS; HSTS am Reverse Proxy. |
| **Logging** | API-Key/Token niemals loggen; strukturierte Logs mit Redaction. |
| **Least Privilege** | Service-Account-Client nur mit `view-users`; intervals-Zugriff immer auf die Athlete-ID aus dem Token beschränkt. |
| **Rate Limiting** | Am Reverse Proxy und/oder im Server, um intervals.icu-Limits zu respektieren. |

---

## 9. Erweiterungen & Roadmap

1. **Self-Service:** Keycloak-Account-Console oder eigene kleine Seite, auf der Nutzer ihren intervals.icu-API-Key selbst eintragen (statt administrativer Pflege).
2. **Schreibende Tools:** Anlegen/Ändern geplanter Workouts (`POST /athlete/{id}/events`), Hochladen von Aktivitäten.
3. **Weitere Datentypen:** Power-Kurven, Fitness/Fatigue/Form (CTL/ATL), benutzerdefinierte Felder.
4. **Caching:** Optionales kurzlebiges Caching häufiger Abfragen zur Entlastung der intervals.icu-API.
5. **Mehrere Identity-Quellen:** Neben Keycloak-Attributen auch Mapping anderer Identitäten.

---

## 10. Umsetzungsschritte (Vorschlag)

1. **Keycloak aufsetzen** (docker-compose: keycloak + postgres + reverse-proxy), Realm `intervals`, Client `claude-mcp`, User-Profile-Attribute, Protocol/Audience-Mapper, Testnutzer mit Attributen.
2. **MCP-Server-Grundgerüst** (FastMCP, Streamable-HTTP), Protected-Resource-Metadata-Endpoint, JWT-Validierung gegen Keycloak-JWKS.
3. **intervals.icu-Client** (`httpx`, Basic Auth) + erstes Tool `list_activities`, End-to-End gegen echten Account testen.
4. **Restliche Tools** (`get_activity`, `get_wellness`, `list_events`, `get_athlete`).
5. **Sicherheitsvariante** entscheiden (API-Key im Token vs. serverseitiger Admin-API-Abruf) und umsetzen.
6. **Härtung & Deployment**: TLS, Secrets, Realm-Import, Logging/Redaction, Rate Limiting.
7. **Verbindungstest mit Claude**: Server als MCP-Connector eintragen, OAuth-Flow durchspielen.
