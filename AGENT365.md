# Agent 365 Onboarding and Block Demonstration

This adds the Microsoft Agent 365 observability SDK through Microsoft's recommended
`microsoft-opentelemetry` distribution. LangChain, Azure OpenAI, the local MCP server,
the blueprint, and the child Agent ID stay unchanged. No Teams bot, mailbox, cloud
hosting, or human token is introduced.

## What Block Means Here

The demonstration targets **Microsoft 365 admin center > Agents > All agents > Block**.
There is no local `blocked=true` switch, Graph polling gate, or hidden Entra
disable operation in the runtime. The portal must enforce its supported policy;
the probe reports what the existing Entra-to-MCP path actually permits.

**Registration and SDK instrumentation do not create a universal remote kill switch.**
Block behavior depends on the registered agent type and tenant capabilities.
Microsoft documents channel availability blocking for several agent types; its
instance-block instructions specifically describe AI teammates. This demo remains
a standard app-only agent, not an AI teammate. Confirm that the registered entry
offers the appropriate Block action before presenting it. If it is absent or a
blocked entry can still obtain fresh tokens, this local path has not demonstrated
enforcement. Do not silently replace it with Entra identity disabling.

## 1. Register the Existing Identity

Your tenant must have Agent 365 enabled, an assigned Agent 365/Microsoft 365 E7
license for observability, and admin access for registration and application-role
grants. Keep the original Entra settings and blueprint credential.

Install dependencies and review the onboarding preview:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Install-Module Microsoft.Graph.Authentication -Scope CurrentUser

.\scripts\onboard_agent365.ps1 `
  -TenantId '<existing-tenant-guid>' `
  -BlueprintClientId '<existing-blueprint-client-guid>' `
  -AgentId '<existing-child-agent-guid>' `
  -GrantObservability -WhatIf
```

Remove `-WhatIf` to register. Setup uses administrator **delegated** Graph access
only at provisioning time. It requests `AgentRegistration.ReadWrite.All`, reads
the existing blueprint/child, and optionally uses `AppRoleAssignment.ReadWrite.All`
plus `Application.Read.All` to grant observability. Grant/consent requires the
appropriate tenant administrator. These admin permissions are never assigned to
the runtime agent.

The script uses the documented **preview** Agent Registration API, posts a card
linked to both existing IDs, and stores only registration IDs in the ignored
`.agent365-registration.json` state file. Reruns verify the saved registration and
reuse the role grant. If a registration already exists, pass `-RegistrationId` to
adopt it. If the state file is lost, locate the existing registry ID before rerunning
to avoid duplicate registrations. A registry success followed by a failed local
state write also requires adopting the returned ID on retry.

`-GrantObservability` assigns the **application** role
`Agent365.Observability.OtelWrite` on resource app
`9b975845-388f-4429-889e-eab1ef63949c` directly to the child identity. If that resource
service principal is missing, an administrator must provision/enable the Agent 365
Observability service in the tenant first. There is no delegated-auth fallback.
Permission propagation can take several minutes.

The registration API records metadata; it does not publish a Teams/Copilot app or
configure an unsupported management integration. Some Agent 365 platform paths
require blueprint `managerApplications`; this script does not guess or grant a
manager application. If the portal reports that requirement, use the tenant's
approved Agent 365 CLI/platform onboarding to configure it for the existing
blueprint. Do not run `a365 setup all` with a new agent name or delete the working
blueprint just to obtain a registry entry.

## 2. Enable SDK Tracing

The original command stays unchanged. Choose a telemetry mode in the agent shell:

```powershell
$env:A365_TELEMETRY_MODE = 'console'
.\.venv\Scripts\python.exe financial_agent.py
```

Expect `invoke_agent`, `chat`, and `execute_tool` spans with the child Agent ID,
blueprint ID and tenant ID. The console mode makes no Agent 365 export request.
There is no fabricated user identity. Model calls and MCP calls remain real.

For cloud export after onboarding:

```powershell
$env:A365_TELEMETRY_MODE = 'agent365'
.\.venv\Scripts\python.exe financial_agent.py
```

Cloud mode uses a **separate two-stage Agent ID token exchange** for
`api://9b975845-388f-4429-889e-eab1ef63949c/.default` and selects the SDK's S2S
export endpoint. The MCP token is never sent to the observability service.
The resolver accepts only the configured child/tenant and refuses an expired cache
entry. This one-shot demo has a three-minute instrumented execution limit and
flushes before exit. A long-running worker would need ongoing observability-token
renewal before subsequent batches.

Tracing captures identity, operation names, model/deployment name, tool name, timing
and sanitized error types. It does not record prompts, responses, tool arguments,
portfolio data, secret values, or HTTP bodies. Automatic HTTP/library tracing,
SDK log/metric export and disk retry storage are disabled. The SDK manual scopes
are attached through LangChain middleware. Do not enable verbose HTTP logging.

In [Microsoft 365 admin center](https://admin.cloud.microsoft/#/agents/all), locate
the entry matching the child ID, then inspect **Activity**. Use Defender/Purview
as available in the tenant. Allow ingestion delay. A successful agent run or HTTP
export is not proof of product visibility; validate the actual activity record.
License assignment, ingestion permissions, auditing, and portal schema requirements
still apply. Metadata-only tracing is not a claim of store-publishing compliance.

Set `A365_TELEMETRY_MODE=off` to retain the original behavior. Existing credentials
and the MCP server configuration do not change between modes.

## 3. Rehearse Allowed, Blocked, Recovered

Keep the local MCP server running. In the agent shell, confirm the same five Entra
settings and Azure OpenAI settings used by the working demo. By default,
[agent365_demo.py](agent365_demo.py) runs the LangChain flow shared with
[financial_agent.py](financial_agent.py): acquire an Agent ID token, discover MCP
tools with `MCPAdapter`, create the agent with `create_agent()`, and invoke it with
`agent.ainvoke()`. The first model turn must call a tool, and success requires a
successful portfolio tool result. `A365_TELEMETRY_MODE` applies to this path too.

To isolate authorization from the model and telemetry, use the optional direct probe:

```powershell
.\.venv\Scripts\python.exe agent365_demo.py --probe-only --expect allowed
```

Only `--probe-only` skips LangChain, Azure OpenAI configuration and observability.
It remains useful for checking a portal block independently of model or export errors.

### Allowed Baseline

```powershell
.\.venv\Scripts\python.exe agent365_demo.py --expect allowed
```

Expect exit code `0`, `mode: langchain`, `outcome: allowed`, `agent_completed: true`,
and `mcp_tool_called: true`, followed by the agent's final summary in `response`.
The LangChain agent requested a fresh token and called the portfolio tool over MCP;
the server's returned portfolio is fictional demo data. Console tracing, when
enabled, also prints SDK spans alongside the JSON result.

### Block in the Portal

1. Open **Agents > All agents** and select the registration for this child identity.
2. Record its name, registry ID and child Agent ID for the demo evidence.
3. Select **Block**, confirm **Block agent**, and save. Record the action time.
4. Allow the control to propagate, then run a **new process**:

```powershell
.\.venv\Scripts\python.exe agent365_demo.py --expect blocked
```

Exit code `0` for this expectation requires the recognized Entra identity-disabled
response `AADSTS7000112`. An initial token denial prevents agent creation and
invocation. On failure, the LangChain result reports `agent_completed: false` and
`mcp_tool_called: null` rather than guessing whether an earlier tool call completed
before a later failure. Use `--probe-only --expect blocked` for the isolated check,
which reports `mcp_tool_called: false` when initial token issuance is denied.
This is evidence of disabled-identity enforcement, not proof of who initiated it.
Correlate the timestamp and Entra correlation ID with the portal/admin audit action.
Other token errors and model, transport, timeout or telemetry failures are not
classified as successful blocking.

If the outcome remains `allowed`, the block is not enforced on this path yet.
Check propagation, the selected registration and linked IDs, and support for that
agent type. If another denial code, MCP error or agent error appears, investigate it
using the sign-in/server logs; the demo deliberately marks it inconclusive instead of
calling every failure a block. Do not disable the identity separately and present
that as the admin-center Block action.

### Unblock and Recover

Choose **Unblock** on the same portal entry, save, allow propagation, then run:

```powershell
$env:A365_TELEMETRY_MODE = 'agent365'
.\.venv\Scripts\python.exe agent365_demo.py --expect allowed
```

Capture restored MCP access and the agent's activity record. Do not rotate secrets,
change the child identity, change MCP validation settings, or modify permissions
between the baseline, blocked and recovered runs.

**Token lifetime caveat:** a running process with an already-issued access token may
continue until expiry. The local MCP server validates JWTs offline and does not
implement continuous access evaluation. Every probe creates a new provider and
forces fresh token issuance. Portal blocking does not terminate the Python process
or revoke the separate Azure OpenAI API key. If blocking prevents observability
token issuance too, use Entra/admin audit evidence for the failed run; do not promise
that a blocked agent can export its own final denial trace.

## Validation Commands

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\tests\test_onboard_agent365.ps1
.\.venv\Scripts\python.exe -m pip check
```

Automated tests use mocks or locally signed test tokens. They do not prove tenant
registration, cloud ingestion, or admin-center enforcement; those are the live
verification steps above. No script in this addition blocks or unblocks an identity.

## References

- [Agent 365 SDK overview](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/agent-365-sdk)
- [Recommended Microsoft OpenTelemetry SDK](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/microsoft-opentelemetry)
- [S2S observability authentication](https://learn.microsoft.com/en-us/microsoft-agent-365/developer/observability-authentication-setup)
- [Agent Registration API](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/admin-settings/agent-registration/overview)
- [Admin-center Block actions](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/agent-actions#block-or-unblock-agents)
- [Instance blocking and supported agent type](https://learn.microsoft.com/en-us/microsoft-365/admin/manage/manage-agent-instances#block-agent-instances)