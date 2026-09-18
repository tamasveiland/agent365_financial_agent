import asyncio
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

import httpx
import httpx2


class TokenAcquisitionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentCredentials:
    tenant_id: str
    blueprint_id: str
    agent_id: str
    client_secret: str = field(repr=False)


class AgentIdentityTokenProvider:
    def __init__(
        self,
        credentials: AgentCredentials,
        scope: str,
        client: httpx.AsyncClient,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.credentials = credentials
        self.scope = scope
        self.client = client
        self.clock = clock
        tenant = str(UUID(credentials.tenant_id))
        self.endpoint = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
        self._token = ""
        self._renew_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            if self._token and self.clock() < self._renew_at:
                return self._token
            started = self.clock()
            exchange_token, _ = await self._request(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.blueprint_id,
                    "client_secret": self.credentials.client_secret,
                    "scope": "api://AzureADTokenExchange/.default",
                    "fmi_path": self.credentials.agent_id,
                },
                "blueprint exchange",
            )
            resource_token, lifetime = await self._request(
                {
                    "grant_type": "client_credentials",
                    "client_id": self.credentials.agent_id,
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": exchange_token,
                    "scope": self.scope,
                },
                "agent resource exchange",
            )
            self._token = resource_token
            self._renew_at = started + max(0.0, lifetime - 60.0)
            return resource_token

    async def _request(self, data: dict[str, str], stage: str) -> tuple[str, float]:
        try:
            response = await self.client.post(
                self.endpoint, data=data, timeout=15.0, follow_redirects=False
            )
        except httpx.HTTPError:
            raise TokenAcquisitionError(f"{stage}: Entra connection failed") from None
        try:
            payload = response.json()
        except ValueError:
            raise TokenAcquisitionError(f"{stage}: invalid Entra response") from None
        if not isinstance(payload, dict):
            raise TokenAcquisitionError(f"{stage}: invalid Entra response")
        if response.status_code != 200:
            code = str(payload.get("error", "unknown_error"))
            code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "unknown_error"
            correlation = payload.get("correlation_id")
            try:
                correlation = str(UUID(str(correlation)))
            except ValueError:
                correlation = "unavailable"
            raise TokenAcquisitionError(
                f"{stage}: HTTP {response.status_code}, {code}, correlation_id={correlation}"
            )
        token = payload.get("access_token")
        try:
            lifetime = float(payload["expires_in"])
        except (KeyError, TypeError, ValueError):
            raise TokenAcquisitionError(f"{stage}: missing token lifetime") from None
        if (
            not isinstance(token, str)
            or not token
            or not math.isfinite(lifetime)
            or lifetime <= 0
            or str(payload.get("token_type", "")).lower() != "bearer"
        ):
            raise TokenAcquisitionError(f"{stage}: invalid token response")
        return token, lifetime


class AgentIdentityAuth(httpx2.Auth):
    def __init__(self, provider: AgentIdentityTokenProvider, resource_url: str) -> None:
        self.provider = provider
        self.resource_url = httpx2.URL(resource_url)

    async def async_auth_flow(self, request: httpx2.Request):
        target = request.url
        expected = self.resource_url
        if (
            (target.scheme, target.host, target.port) !=
            (expected.scheme, expected.host, expected.port)
            or target.path.rstrip("/") != expected.path.rstrip("/")
        ):
            raise TokenAcquisitionError("Refusing to send an MCP token to another resource")
        request.headers["Authorization"] = f"Bearer {await self.provider.get_token()}"
        yield request