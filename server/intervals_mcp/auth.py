"""Keycloak JWT token verifier (OAuth 2.1 resource server).

The MCP server acts as an OAuth 2.1 *resource server*: it validates the bearer
access token issued by Keycloak and extracts the intervals.icu credentials that
Keycloak embeds as custom claims (Variant 1 — both athlete id and API key live in
the token, written by a Keycloak protocol mapper from user attributes).
"""

from __future__ import annotations

import logging

import anyio
import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .config import Settings

logger = logging.getLogger(__name__)


class KeycloakTokenVerifier(TokenVerifier):
    """Verifies RS256 JWTs minted by Keycloak and surfaces their claims."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._jwks_uri, self._issuer = self._discover(settings.oidc_issuer)
        # PyJWKClient caches signing keys and refreshes on key rotation.
        self._jwk_client = PyJWKClient(self._jwks_uri, cache_keys=True)

    @staticmethod
    def _discover(issuer: str) -> tuple[str, str]:
        """Fetch OIDC discovery document to locate the JWKS endpoint."""
        well_known = f"{issuer}/.well-known/openid-configuration"
        resp = httpx.get(well_known, timeout=10.0)
        resp.raise_for_status()
        doc = resp.json()
        return doc["jwks_uri"], doc.get("issuer", issuer)

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            signing_key = await anyio.to_thread.run_sync(
                self._jwk_client.get_signing_key_from_jwt, token
            )
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._settings.audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat"]},
            )
        except Exception as exc:  # noqa: BLE001 - any failure = unauthenticated
            logger.info("Token verification failed: %s", exc)
            return None

        scopes = str(claims.get("scope", "")).split()
        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("client_id") or "",
            scopes=scopes,
            expires_at=claims.get("exp"),
            subject=claims.get("sub"),
            # resource_server_url equality is enforced by the bearer middleware
            # only when validate_token_resource is set; audience is already
            # verified above, so we tag the token with our own resource URL.
            resource=self._settings.resource_url,
            claims=claims,
        )
