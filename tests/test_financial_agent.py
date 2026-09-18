import unittest
from unittest.mock import AsyncMock, patch

import httpx2
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from mcp.server.auth.provider import AccessToken
from pydantic import Field

from agent365_demo import run_demo
from config import AgentSettings, ServerSettings
from entra_agent_auth import AgentCredentials, AgentIdentityAuth, TokenAcquisitionError
from financial_agent import run_agent
from mcp_server import create_server


class DemoModel(BaseChatModel):
    choices: list = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "test-model"

    def bind_tools(self, tools, **kwargs):
        return self.bind(**kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.choices.append(kwargs.get("tool_choice"))
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content=f"Mock portfolio: {messages[-1].content}")
        else:
            message = AIMessage(content="", tool_calls=[{
                "name": "get_portfolio_summary", "args": {"account_id": "DEMO-001"},
                "id": "portfolio-call", "type": "tool_call",
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_token_denial_stops_before_mcp_and_model(self):
        settings = AgentSettings(ServerSettings(
            "11111111-1111-1111-1111-111111111111",
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        ), AgentCredentials(
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333", "test-secret",
        ), "test-deployment")
        with patch("financial_agent.AgentIdentityTokenProvider.get_token",
                   new_callable=AsyncMock,
                   side_effect=TokenAcquisitionError("agent resource exchange: access denied",
                                                     error_codes=(7000112,))), patch(
            "financial_agent.MCPAdapter"
        ) as adapter, patch("financial_agent.init_chat_model") as model:
            with self.assertRaises(TokenAcquisitionError):
                await run_agent(settings)
            result = await run_demo(settings)
            self.assertEqual(result["outcome"], "identity_disabled")
            self.assertFalse(result["agent_completed"])
        adapter.assert_not_called()
        model.assert_not_called()

    async def test_real_mcp_transport_and_agent_graph(self):
        server_settings = ServerSettings(
            "11111111-1111-1111-1111-111111111111",
            "33333333-3333-3333-3333-333333333333",
            "44444444-4444-4444-4444-444444444444",
        )
        settings = AgentSettings(server_settings, AgentCredentials(
            server_settings.tenant_id, "22222222-2222-2222-2222-222222222222",
            server_settings.agent_id, "test-secret",
        ), "test-deployment")
        verifier = AsyncMock()
        verifier.verify_token.return_value = AccessToken(
            token="test-token", client_id=server_settings.agent_id,
            scopes=[server_settings.role],
        )
        app = create_server(server_settings, verifier).streamable_http_app(
            stateless_http=True, json_response=True
        )
        requests = []

        async def record(request):
            requests.append(request)

        def client_factory(**kwargs):
            kwargs["follow_redirects"] = False
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                event_hooks={"request": [record]}, **kwargs,
            )

        model = DemoModel()
        async with app.router.lifespan_context(app):
            with patch("financial_agent.mcp_http_client", client_factory), patch(
                "financial_agent.AgentIdentityTokenProvider.get_token",
                new_callable=AsyncMock, return_value="test-token",
            ):
                result = await run_agent(settings, model)
                with patch("financial_agent.init_chat_model", return_value=model) as initialize:
                    evidence = await run_demo(settings)
                initialize.assert_called_once()
        self.assertIn("20000", result)
        self.assertEqual(evidence["mode"], "langchain")
        self.assertEqual(evidence["outcome"], "allowed")
        self.assertTrue(evidence["agent_completed"])
        self.assertTrue(evidence["mcp_tool_called"])
        self.assertIn("20000", evidence["response"])
        self.assertEqual(model.choices[0], "required")
        self.assertTrue(requests)
        self.assertTrue(all(request.headers.get("Authorization") == "Bearer test-token"
                            for request in requests))
        verifier.verify_token.assert_awaited_with("test-token")

    async def test_auth_never_sends_token_to_another_resource(self):
        provider = AsyncMock()
        provider.get_token.return_value = "private-token"
        auth = AgentIdentityAuth(provider, "http://127.0.0.1:8000/mcp")
        for url in ["https://example.org/mcp", "http://127.0.0.1:8000/other"]:
            async with httpx2.AsyncClient(auth=auth, transport=httpx2.MockTransport(
                lambda request: httpx2.Response(200)
            )) as client:
                with self.assertRaises(TokenAcquisitionError):
                    await client.get(url)
        provider.get_token.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()