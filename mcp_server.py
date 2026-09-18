import logging

from mcp.server import MCPServer
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from config import ServerSettings
from entra_token_verifier import EntraTokenVerifier


def get_portfolio_summary(account_id: str) -> dict:
    """Return read-only, fictional holdings for the demo account DEMO-001 in USD."""
    if account_id != "DEMO-001":
        raise ValueError("Unknown demo account")
    holdings = [
        {"symbol": "DEMO-EQUITY", "quantity": 100, "price_usd": 125},
        {"symbol": "DEMO-BOND", "quantity": 50, "price_usd": 100},
    ]
    cash_usd = 2500
    return {
        "account_id": account_id,
        "currency": "USD",
        "data_source": "fictional demo fixtures, not live market data",
        "holdings": holdings,
        "cash_usd": cash_usd,
        "total_value_usd": cash_usd + sum(
            holding["quantity"] * holding["price_usd"] for holding in holdings
        ),
    }


def create_server(
    settings: ServerSettings, verifier: TokenVerifier | None = None
) -> MCPServer:
    server = MCPServer(
        "Protected Portfolio Demo",
        token_verifier=verifier if verifier is not None else EntraTokenVerifier(settings),
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(settings.issuer),
            resource_server_url=AnyHttpUrl(settings.url),
            required_scopes=[settings.role],
            validate_token_resource=False,
        ),
    )
    server.tool()(get_portfolio_summary)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = ServerSettings.from_env()
    logging.info("Serving protected MCP at %s; required role=%s", settings.url, settings.role)
    create_server(settings).run(
        transport="streamable-http", host=settings.host, port=settings.port,
        stateless_http=True, json_response=True,
    )


if __name__ == "__main__":
    main()