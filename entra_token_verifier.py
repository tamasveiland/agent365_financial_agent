import asyncio
import logging
from urllib.parse import urlparse

import httpx
import jwt
from mcp.server.auth.provider import AccessToken

from config import ServerSettings


logger = logging.getLogger(__name__)


class EntraTokenVerifier:
    def __init__(self, settings: ServerSettings) -> None:
        self.settings = settings
        self._keys: jwt.PyJWKClient | None = None
        self._lock = asyncio.Lock()

    async def _discover(self) -> jwt.PyJWKClient:
        async with self._lock:
            if self._keys is not None:
                return self._keys
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
                response = await client.get(
                    f"{self.settings.issuer}/.well-known/openid-configuration"
                )
                response.raise_for_status()
                metadata = response.json()
            if metadata["issuer"] != self.settings.issuer:
                raise ValueError("Unexpected discovery issuer")
            uri = metadata["jwks_uri"]
            if not isinstance(uri, str):
                raise ValueError("Invalid signing key URL")
            parsed = urlparse(uri)
            if parsed.scheme != "https" or parsed.netloc != "login.microsoftonline.com":
                raise ValueError("Unexpected signing key authority")
            self._keys = jwt.PyJWKClient(uri, timeout=10, lifespan=300)
            return self._keys

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            keys = await self._discover()
            signing_key = await asyncio.to_thread(keys.get_signing_key_from_jwt, token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.settings.api_id,
                issuer=self.settings.issuer,
                leeway=30,
                options={"require": ["exp", "nbf", "iat", "iss", "aud", "tid",
                                     "oid", "azp", "idtyp", "ver"]},
            )
            if (
                claims["tid"] != self.settings.tenant_id
                or claims["oid"] != self.settings.agent_id
                or claims["azp"] != self.settings.agent_id
                or claims["idtyp"] != "app"
                or claims["ver"] != "2.0"
                or "scp" in claims
            ):
                return None
            roles = claims.get("roles", [])
            if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
                return None
            if self.settings.role in roles:
                logger.info("Authorized agent=%s tenant=%s role=%s",
                            claims["oid"], claims["tid"], self.settings.role)
            return AccessToken(
                token=token,
                client_id=claims["azp"],
                subject=claims["oid"],
                scopes=roles,
                expires_at=claims["exp"],
                resource=self.settings.api_id,
            )
        except (jwt.PyJWTError, httpx.HTTPError, ValueError, KeyError, TypeError, OSError):
            logger.warning("Bearer token rejected: signature, claims, or key discovery failed")
            return None