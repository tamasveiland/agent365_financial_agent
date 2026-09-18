import os
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from microsoft.opentelemetry.a365.core.opentelemetry_scope import OpenTelemetryScope
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent365_demo import exit_code, main, probe, run_demo
from agent365_observability import Agent365Session, configure_tracing
from config import AgentSettings, ServerSettings
from entra_agent_auth import AgentCredentials, TokenAcquisitionError


SETTINGS = AgentSettings(ServerSettings(
    "11111111-1111-1111-1111-111111111111",
    "33333333-3333-3333-3333-333333333333",
    "44444444-4444-4444-4444-444444444444",
), AgentCredentials(
    "11111111-1111-1111-1111-111111111111",
    "22222222-2222-2222-2222-222222222222",
    "33333333-3333-3333-3333-333333333333", "private-secret",
), "test-model", "console")


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.enterContext(patch.object(OpenTelemetryScope, "_get_tracer",
                                      return_value=self.provider.get_tracer("test")))
        self.enterContext(patch.object(OpenTelemetryScope, "_is_telemetry_enabled", return_value=True))
        self.addCleanup(self.provider.shutdown)

    def test_root_identity_and_no_fictitious_user(self):
        session = Agent365Session(SETTINGS)
        with session.invocation():
            pass
        span = self.exporter.get_finished_spans()[0]
        self.assertIsNone(span.parent)
        self.assertEqual(span.attributes["gen_ai.operation.name"], "invoke_agent")
        self.assertEqual(span.attributes["gen_ai.agent.id"], SETTINGS.server.agent_id)
        self.assertEqual(span.attributes["microsoft.tenant.id"], SETTINGS.server.tenant_id)
        self.assertNotIn("user.id", span.attributes)
        self.assertNotIn("gen_ai.input.messages", span.attributes)

    def test_error_payload_is_not_exported(self):
        with self.assertRaises(ValueError):
            with Agent365Session(SETTINGS).invocation():
                raise ValueError("private-secret access-token prompt-data")
        span = self.exporter.get_finished_spans()[0]
        exported = span.to_json()
        self.assertNotIn("private-secret", exported)
        self.assertNotIn("access-token", exported)
        self.assertNotIn("prompt-data", exported)
        self.assertEqual(span.status.status_code.name, "ERROR")

    def test_configuration_uses_s2s_and_disables_payload_capture(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "agent365_observability.use_microsoft_opentelemetry"
        ) as configure:
            configure_tracing(SETTINGS, lambda agent, tenant: None)
        options = configure.call_args.kwargs
        self.assertTrue(options["a365_use_s2s_endpoint"])
        self.assertFalse(options["a365_enable_observability_exporter"])
        self.assertTrue(options["enable_console"])
        self.assertFalse(options["enable_sensitive_data"])
        self.assertTrue(options["disable_logging"])
        self.assertTrue(all(not item["enabled"] for item in options["instrumentation_options"].values()))


class DemoTests(unittest.IsolatedAsyncioTestCase):
    async def test_langchain_demo_returns_agent_response(self):
        with patch("agent365_demo.run_with_observability", new_callable=AsyncMock,
                   return_value="Mock portfolio total: $20,000 USD") as run:
            result = await run_demo(SETTINGS)
        run.assert_awaited_once_with(SETTINGS)
        self.assertEqual(result["mode"], "langchain")
        self.assertTrue(result["agent_completed"])
        self.assertTrue(result["mcp_tool_called"])
        self.assertIn("20,000", result["response"])
        self.assertEqual(exit_code(result, "allowed"), 0)

    async def test_langchain_failures_are_not_misreported_as_blocking(self):
        for error, expected in [
            (TokenAcquisitionError("disabled", error_codes=(7000112,)), "identity_disabled"),
            (TokenAcquisitionError("bad secret", error_codes=(7000215,)), "token_error"),
            (RuntimeError("private model response"), "agent_error"),
            (TimeoutError("private timeout"), "agent_error"),
        ]:
            with self.subTest(expected=expected), patch(
                "agent365_demo.run_with_observability", new_callable=AsyncMock, side_effect=error
            ):
                result = await run_demo(SETTINGS)
            self.assertEqual(result["outcome"], expected)
            self.assertFalse(result["agent_completed"])
            self.assertIsNone(result["mcp_tool_called"])
            self.assertNotIn("private", str(result))
            self.assertEqual(exit_code(result, "blocked"), 0 if expected == "identity_disabled" else 1)

    async def test_disabled_identity_never_reaches_mcp(self):
        with patch("agent365_demo.AgentIdentityTokenProvider.get_token", new_callable=AsyncMock,
                   side_effect=TokenAcquisitionError("sanitized denial", error_codes=(7000112,))), patch(
            "agent365_demo.Client"
        ) as client:
            result = await probe(SETTINGS.server, SETTINGS.credentials)
        self.assertEqual(result["outcome"], "identity_disabled")
        self.assertEqual(exit_code(result, "blocked"), 0)
        self.assertFalse(result["mcp_tool_called"])
        client.assert_not_called()

    async def test_bad_secret_is_not_reported_as_block(self):
        with patch("agent365_demo.AgentIdentityTokenProvider.get_token", new_callable=AsyncMock,
                   side_effect=TokenAcquisitionError("invalid_client", error_codes=(7000215,))):
            result = await probe(SETTINGS.server, SETTINGS.credentials)
        self.assertEqual(result["outcome"], "token_error")
        self.assertNotEqual(exit_code(result, "blocked"), 0)

    async def test_allowed_and_mcp_offline_are_distinct(self):
        with patch("agent365_demo.AgentIdentityTokenProvider.get_token", new_callable=AsyncMock,
                   return_value="never-printed"), patch("agent365_demo.Client") as client:
            instance = client.return_value.__aenter__.return_value
            instance.call_tool.return_value = SimpleNamespace(is_error=False)
            result = await probe(SETTINGS.server, SETTINGS.credentials)
            self.assertEqual(result["outcome"], "allowed")
            self.assertTrue(result["mcp_tool_called"])
            self.assertNotEqual(exit_code(result, "blocked"), 0)
            instance.call_tool.side_effect = ConnectionError("sensitive payload")
            result = await probe(SETTINGS.server, SETTINGS.credentials)
            self.assertEqual(result["outcome"], "mcp_error")
            self.assertNotIn("sensitive payload", str(result))
            self.assertNotEqual(exit_code(result, "blocked"), 0)

    async def test_sdk_middleware_records_child_spans_without_payloads(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        session = Agent365Session(SETTINGS)
        model_middleware, tool_middleware = session.middleware()
        try:
            with patch.object(OpenTelemetryScope, "_get_tracer", return_value=provider.get_tracer("test")), patch.object(
                OpenTelemetryScope, "_is_telemetry_enabled", return_value=True
            ), session.invocation():
                await model_middleware.awrap_model_call(SimpleNamespace(), AsyncMock(return_value="private response"))
                await tool_middleware.awrap_tool_call(SimpleNamespace(tool_call={
                    "name": "get_portfolio_summary", "id": "call-1", "args": {"secret": "private args"},
                }), AsyncMock(return_value="private portfolio"))
            spans = exporter.get_finished_spans()
            root = next(span for span in spans if span.attributes["gen_ai.operation.name"] == "invoke_agent")
            children = [span for span in spans if span is not root]
            self.assertEqual({span.attributes["gen_ai.operation.name"] for span in children}, {"chat", "execute_tool"})
            self.assertTrue(all(span.parent.span_id == root.context.span_id for span in children))
            self.assertNotIn("private", "".join(span.to_json() for span in spans))
        finally:
            provider.shutdown()


class DemoCommandTests(unittest.TestCase):
    def test_default_command_runs_langchain(self):
        output = io.StringIO()
        with patch("sys.argv", ["agent365_demo.py", "--expect", "allowed"]), patch(
            "agent365_demo.AgentSettings.from_env", return_value=SETTINGS
        ), patch("agent365_demo.run_demo", new_callable=AsyncMock,
                 return_value={"outcome": "allowed", "response": "portfolio summary"}) as run, patch(
            "agent365_demo.probe", new_callable=AsyncMock
        ) as direct_probe, patch("sys.stdout", output), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 0)
        run.assert_awaited_once_with(SETTINGS)
        direct_probe.assert_not_awaited()
        self.assertEqual(json.loads(output.getvalue())["response"], "portfolio summary")

    def test_probe_only_does_not_require_model_configuration(self):
        with patch("sys.argv", ["agent365_demo.py", "--probe-only"]), patch(
            "agent365_demo.ServerSettings.from_env", return_value=SETTINGS.server
        ), patch("agent365_demo.identifier", return_value=SETTINGS.credentials.blueprint_id), patch(
            "agent365_demo.required", return_value="test-secret"
        ), patch("agent365_demo.probe", new_callable=AsyncMock,
                 return_value={"outcome": "allowed"}) as direct_probe, patch(
            "agent365_demo.AgentSettings.from_env"
        ) as model_settings, patch("sys.stdout", io.StringIO()), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 0)
        direct_probe.assert_awaited_once()
        model_settings.assert_not_called()