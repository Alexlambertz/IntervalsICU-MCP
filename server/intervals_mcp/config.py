"""Runtime configuration, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


@dataclass(frozen=True)
class Settings:
    # --- OAuth / Keycloak (resource server) ---
    oidc_issuer: str
    """Keycloak realm issuer, e.g. https://auth.example.com/realms/intervals"""
    audience: str
    """Expected `aud` claim identifying this MCP server (Keycloak audience mapper)."""
    resource_url: str
    """Public base URL of this MCP server, published in protected-resource metadata."""

    # --- Claim names carrying the intervals.icu credentials (Variant 1) ---
    athlete_claim: str
    apikey_claim: str

    # --- OAuth scopes advertised in protected-resource metadata + required on
    #     tokens. Advertising a concrete list keeps MCP clients from falling
    #     back to the authorization server's full scope catalog. ---
    scopes: tuple[str, ...]

    # --- intervals.icu API ---
    intervals_base_url: str

    # --- HTTP transport ---
    host: str
    port: int
    mcp_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            oidc_issuer=_env("OIDC_ISSUER", required=True).rstrip("/"),
            audience=_env("MCP_AUDIENCE", "intervals-mcp-server"),
            resource_url=_env("MCP_RESOURCE_URL", required=True).rstrip("/"),
            athlete_claim=_env("ATHLETE_CLAIM", "intervals_athlete_id"),
            apikey_claim=_env("APIKEY_CLAIM", "intervals_api_key"),
            scopes=tuple(
                s for s in _env("MCP_SCOPES", "openid").replace(",", " ").split() if s
            ),
            intervals_base_url=_env(
                "INTERVALS_BASE_URL", "https://intervals.icu/api/v1"
            ).rstrip("/"),
            host=_env("HOST", "0.0.0.0"),
            port=int(_env("PORT", "8000")),
            mcp_path=_env("MCP_PATH", "/mcp"),
        )
