import asyncio
import os
from contextlib import asynccontextmanager, contextmanager
from uuid import uuid4

import httpx
from langchain.agents.middleware import wrap_model_call, wrap_tool_call
from microsoft.opentelemetry import use_microsoft_opentelemetry
from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    BaggageBuilder,
    Channel,
    ExecuteToolScope,
    InferenceCallDetails,
    InferenceOperationType,
    InferenceScope,
    InvokeAgentScope,
    InvokeAgentScopeDetails,
    Request,
    ServiceEndpoint,
    ToolCallDetails,
)
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource

from config import AgentSettings
from entra_agent_auth import AgentIdentityTokenProvider


OBSERVABILITY_SCOPE = "api://9b975845-388f-4429-889e-eab1ef63949c/.default"


@contextmanager
def safe_scope(scope):
    scope.__enter__()
    try:
        yield scope
    except Exception as error:
        scope.record_error(RuntimeError(type(error).__name__))
        raise
    finally:
        scope.__exit__(None, None, None)


class Agent365Session:
    def __init__(self, settings: AgentSettings) -> None:
        self.settings = settings
        self.run_id = str(uuid4())
        self.agent_details = AgentDetails(
            agent_id=settings.server.agent_id,
            agent_name="UnattendedPortfolioAgent",
            agent_blueprint_id=settings.credentials.blueprint_id,
            tenant_id=settings.server.tenant_id,
            provider_name="langchain",
        )
        self.request = Request(
            session_id=self.run_id, conversation_id=self.run_id,
            channel=Channel(name="scheduled-job"),
        )

    @contextmanager
    def invocation(self):
        with (
            BaggageBuilder().tenant_id(self.settings.server.tenant_id)
            .agent_id(self.settings.server.agent_id).conversation_id(self.run_id).build(),
            safe_scope(InvokeAgentScope.start(
                self.request, InvokeAgentScopeDetails(), self.agent_details
            )),
        ):
            yield

    def middleware(self) -> list:
        @wrap_model_call
        async def trace_model(request, handler):
            details = InferenceCallDetails(
                operationName=InferenceOperationType.CHAT,
                model=self.settings.deployment, providerName="azure-openai",
            )
            with safe_scope(InferenceScope.start(self.request, details, self.agent_details)) as scope:
                scope.set_tag_maybe("gen_ai.operation.name", "chat")
                return await handler(request)

        @wrap_tool_call
        async def trace_tool(request, handler):
            details = ToolCallDetails(
                tool_name=request.tool_call["name"],
                tool_call_id=request.tool_call["id"],
                tool_type="function",
                endpoint=ServiceEndpoint(
                    hostname=self.settings.server.host, port=self.settings.server.port,
                ),
            )
            with safe_scope(ExecuteToolScope.start(self.request, details, self.agent_details)):
                return await handler(request)

        return [trace_model, trace_tool]


def configure_tracing(settings: AgentSettings, token_resolver) -> None:
    if any(name.startswith("OTEL_EXPORTER_OTLP") and name.endswith("ENDPOINT")
           and value for name, value in os.environ.items()):
        raise ValueError("Unset OTEL_EXPORTER_OTLP endpoint settings for this Agent 365 demo")
    for name in ("ENABLE_OBSERVABILITY", "ENABLE_A365_OBSERVABILITY"):
        if os.getenv(name, "").lower() in {"false", "0", "no", "off"}:
            raise ValueError(f"{name} disables the requested Agent 365 tracing")
    libraries = (
        "django", "fastapi", "flask", "httpx", "httpx2", "psycopg2", "requests",
        "urllib", "urllib3", "langchain", "openai", "openai_agents", "azure_sdk",
        "semantic_kernel", "agent_framework",
    )
    use_microsoft_opentelemetry(
        enable_a365=True,
        enable_azure_monitor=False,
        enable_console=settings.telemetry_mode == "console",
        disable_logging=True,
        disable_metrics=True,
        enable_sensitive_data=False,
        a365_enable_observability_exporter=settings.telemetry_mode == "agent365",
        a365_token_resolver=token_resolver,
        a365_use_s2s_endpoint=True,
        a365_suppress_invoke_agent_input=True,
        a365_exporter_disable_offline_storage=True,
        instrumentation_options={library: {"enabled": False} for library in libraries},
        resource=Resource.create({"service.name": "UnattendedPortfolioAgent"}),
    )


@asynccontextmanager
async def agent365_session(settings: AgentSettings):
    if settings.telemetry_mode == "off":
        yield None
        return
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as client:
        provider = AgentIdentityTokenProvider(settings.credentials, OBSERVABILITY_SCOPE, client)
        if settings.telemetry_mode == "agent365":
            await provider.get_token()

        def resolve_token(agent_id: str, tenant_id: str) -> str | None:
            if (agent_id, tenant_id) != (settings.server.agent_id, settings.server.tenant_id):
                return None
            return provider.cached_token()

        configure_tracing(settings, resolve_token)
        try:
            session = Agent365Session(settings)
            with session.invocation():
                yield session
        finally:
            tracer_provider = trace.get_tracer_provider()
            if hasattr(tracer_provider, "force_flush"):
                await asyncio.to_thread(tracer_provider.force_flush, timeout_millis=15000)
            if hasattr(tracer_provider, "shutdown"):
                await asyncio.to_thread(tracer_provider.shutdown)