import os
from dataclasses import dataclass, field
from uuid import UUID

from dotenv import load_dotenv

from entra_agent_auth import AgentCredentials


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Set {name} before starting the demo")
    return value


def identifier(name: str) -> str:
    try:
        return str(UUID(required(name)))
    except ValueError:
        raise ValueError(f"{name} must be a GUID") from None


@dataclass(frozen=True)
class ServerSettings:
    tenant_id: str
    agent_id: str
    api_id: str
    host: str = "127.0.0.1"
    port: int = 8000
    role: str = "Portfolio.Read"

    @property
    def issuer(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def scope(self) -> str:
        return f"api://{self.api_id}/.default"

    @classmethod
    def from_env(cls) -> "ServerSettings":
        load_dotenv()
        host = os.getenv("MCP_HOST", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("This demo only supports loopback MCP_HOST values")
        try:
            port = int(os.getenv("MCP_PORT", "8000"))
        except ValueError:
            raise ValueError("MCP_PORT must be an integer") from None
        if not 1 <= port <= 65535:
            raise ValueError("MCP_PORT must be between 1 and 65535")
        return cls(identifier("ENTRA_TENANT_ID"), identifier("ENTRA_AGENT_ID"),
                   identifier("MCP_API_CLIENT_ID"), host, port)


@dataclass(frozen=True)
class AgentSettings:
    server: ServerSettings
    credentials: AgentCredentials = field(repr=False)
    deployment: str
    telemetry_mode: str = "off"

    @classmethod
    def from_env(cls) -> "AgentSettings":
        server = ServerSettings.from_env()
        credentials = AgentCredentials(
            server.tenant_id, identifier("ENTRA_BLUEPRINT_CLIENT_ID"),
            server.agent_id, required("ENTRA_BLUEPRINT_CLIENT_SECRET"),
        )
        required("AZURE_OPENAI_ENDPOINT")
        required("AZURE_OPENAI_API_KEY")
        mode = os.getenv("A365_TELEMETRY_MODE", "off").lower()
        if mode not in {"off", "console", "agent365"}:
            raise ValueError("A365_TELEMETRY_MODE must be off, console, or agent365")
        return cls(server, credentials, required("AZURE_OPENAI_DEPLOYMENT_NAME"), mode)