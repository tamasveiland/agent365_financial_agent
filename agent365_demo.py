import argparse
import asyncio
import json
from datetime import datetime, timezone

import httpx
import httpx2
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from config import AgentSettings, ServerSettings, identifier, required
from entra_agent_auth import AgentCredentials, AgentIdentityAuth, AgentIdentityTokenProvider, TokenAcquisitionError
from financial_agent import run_with_observability


def demo_http_client(**kwargs) -> httpx2.AsyncClient:
    kwargs["follow_redirects"] = False
    kwargs.setdefault("timeout", httpx2.Timeout(15.0))
    return httpx2.AsyncClient(**kwargs)


async def probe(settings: ServerSettings, credentials: AgentCredentials) -> dict:
    evidence = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tenant_id": settings.tenant_id,
        "agent_id": settings.agent_id,
        "blueprint_id": credentials.blueprint_id,
        "fresh_token_request": True,
        "mcp_tool_called": False,
    }
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as identity_client:
        provider = AgentIdentityTokenProvider(credentials, settings.scope, identity_client)
        try:
            await provider.get_token()
        except TokenAcquisitionError as error:
            return evidence | {
                "outcome": "identity_disabled" if 7000112 in error.error_codes else "token_error",
                "detail": str(error),
            }
        transport = StreamableHttpTransport(
            settings.url, auth=AgentIdentityAuth(provider, settings.url),
            httpx_client_factory=demo_http_client,
        )
        try:
            async with Client(transport, timeout=15) as client:
                result = await client.call_tool("get_portfolio_summary", {"account_id": "DEMO-001"})
                if result.is_error:
                    return evidence | {"outcome": "tool_error"}
        except Exception as error:
            return evidence | {"outcome": "mcp_error", "error_type": type(error).__name__}
    return evidence | {"outcome": "allowed", "mcp_tool_called": True}


async def run_demo(settings: AgentSettings) -> dict:
    evidence = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "tenant_id": settings.server.tenant_id,
        "agent_id": settings.server.agent_id,
        "blueprint_id": settings.credentials.blueprint_id,
        "mode": "langchain",
        "fresh_token_request": True,
        "agent_completed": False,
        "mcp_tool_called": None,
    }
    try:
        response = await asyncio.wait_for(run_with_observability(settings), timeout=240)
    except TokenAcquisitionError as error:
        return evidence | {
            "outcome": "identity_disabled" if 7000112 in error.error_codes else "token_error",
            "detail": str(error),
        }
    except Exception as error:
        return evidence | {"outcome": "agent_error", "error_type": type(error).__name__}
    return evidence | {
        "outcome": "allowed",
        "agent_completed": True,
        "mcp_tool_called": True,
        "response": response,
    }


def exit_code(evidence: dict, expected: str) -> int:
    if expected == "blocked":
        return 0 if evidence["outcome"] == "identity_disabled" else 1
    if expected == "allowed":
        return 0 if evidence["outcome"] == "allowed" else 1
    return 0 if evidence["outcome"] in {"allowed", "identity_disabled"} else 2


def main() -> None:
    parser = argparse.ArgumentParser(description=(
        "Run a LangChain agent with fresh Agent ID authentication and MCP portfolio tools. "
        "Portal Block must be performed separately; no local block simulation is used."
    ))
    parser.add_argument("--probe-only", action="store_true",
                        help="Call MCP directly without LangChain, Azure OpenAI, or telemetry")
    parser.add_argument("--expect", choices=["allowed", "blocked", "either"], default="either",
                        help="blocked requires Entra AADSTS7000112; other failures are inconclusive")
    arguments = parser.parse_args()
    try:
        if arguments.probe_only:
            settings = ServerSettings.from_env()
            credentials = AgentCredentials(
                settings.tenant_id, identifier("ENTRA_BLUEPRINT_CLIENT_ID"), settings.agent_id,
                required("ENTRA_BLUEPRINT_CLIENT_SECRET"),
            )
            evidence = asyncio.run(asyncio.wait_for(probe(settings, credentials), timeout=60))
            evidence["mode"] = "probe"
        else:
            evidence = asyncio.run(run_demo(AgentSettings.from_env()))
    except Exception as error:
        evidence = {"outcome": "configuration_or_transport_error", "error_type": type(error).__name__}
    print(json.dumps(evidence | {"expected": arguments.expect}, indent=2))
    raise SystemExit(exit_code(evidence, arguments.expect))


if __name__ == "__main__":
    main()