#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [Parameter(Mandatory)][guid]$BlueprintClientId,
    [Parameter(Mandatory)][guid]$AgentId,
    [string]$DisplayName = 'Unattended Portfolio Agent',
    [string]$RegistrationId,
    [string]$StatePath = (Join-Path $PSScriptRoot '../.agent365-registration.json'),
    [switch]$GrantObservability
)

$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess(
    "$AgentId in tenant $TenantId",
    "Register existing Agent ID with Agent 365 (observability grant: $GrantObservability)"
)) { return }
if (-not (Get-Module -ListAvailable Microsoft.Graph.Authentication)) {
    throw 'Install-Module Microsoft.Graph.Authentication -Scope CurrentUser first.'
}
Import-Module Microsoft.Graph.Authentication
$scopes = @('User.Read', 'AgentRegistration.ReadWrite.All',
    'AgentIdentity.Read.All', 'AgentIdentityBlueprint.Read.All')
if ($GrantObservability) {
    $scopes += @('Application.Read.All', 'AppRoleAssignment.ReadWrite.All')
}
Connect-MgGraph -TenantId $TenantId -Scopes $scopes -ContextScope Process -NoWelcome

function Invoke-OnboardingGraph {
    param([string]$Method = 'GET', [string]$Path, [hashtable]$Body)
    $parameters = @{
        Method = $Method
        Uri = "https://graph.microsoft.com/$Path"
        Headers = @{ 'OData-Version' = '4.0' }
        OutputType = 'Hashtable'
    }
    if ($Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 15
        $parameters.ContentType = 'application/json'
    }
    Invoke-MgGraphRequest @parameters
}

function Get-OnboardingItems {
    param([string]$Path)
    do {
        $page = Invoke-OnboardingGraph -Path $Path
        @($page.value)
        $Path = $null
        if ($page['@odata.nextLink']) {
            $next = [uri]$page['@odata.nextLink']
            if ($next.Scheme -ne 'https' -or $next.Host -ne 'graph.microsoft.com') {
                throw 'Unexpected pagination authority.'
            }
            $Path = $next.PathAndQuery.TrimStart('/')
        }
    } while ($Path)
}

$blueprint = Invoke-OnboardingGraph -Path "v1.0/applications(appId='$BlueprintClientId')/microsoft.graph.agentIdentityBlueprint"
$agent = Invoke-OnboardingGraph -Path "v1.0/servicePrincipals/$AgentId/microsoft.graph.agentIdentity"
if ($agent.agentIdentityBlueprintId -ne $BlueprintClientId.ToString()) {
    throw 'The child agent does not belong to the supplied blueprint. No changes made.'
}
if (Test-Path $StatePath) {
    $state = Get-Content -Raw $StatePath | ConvertFrom-Json -AsHashtable
    if ($state.tenantId -ne $TenantId.ToString() -or
        $state.blueprintClientId -ne $BlueprintClientId.ToString() -or
        $state.agentId -ne $AgentId.ToString()) {
        throw 'Registration state belongs to another identity; use a different -StatePath.'
    }
    if ($RegistrationId -and $RegistrationId -ne $state.registrationId) {
        throw 'RegistrationId differs from the saved registration state.'
    }
    $RegistrationId = $state.registrationId
}

if ($RegistrationId) {
    $encodedId = [uri]::EscapeDataString($RegistrationId)
    $registration = Invoke-OnboardingGraph -Path "beta/copilot/agentRegistrations/$encodedId"
    if ($registration.agentIdentityId -ne $AgentId.ToString() -or
        $registration.agentIdentityBlueprintId -ne $BlueprintClientId.ToString()) {
        throw 'Registry entry does not match the existing blueprint and child identity.'
    }
    Write-Host 'Existing Agent 365 registration verified; metadata was not overwritten.'
} else {
    $owner = Invoke-OnboardingGraph -Path 'v1.0/me?$select=id'
    $createdAt = if ($agent.createdDateTime) { $agent.createdDateTime } else { [datetimeoffset]::UtcNow.ToString('o') }
    $registration = Invoke-OnboardingGraph -Method POST -Path 'beta/copilot/agentRegistrations' -Body @{
        displayName = $DisplayName
        description = 'Unattended, read-only portfolio demo using Agent ID and a local MCP server.'
        ownerIds = @($owner.id)
        createdBy = $owner.id
        sourceAgentId = $AgentId.ToString()
        originatingStore = 'LocalLangChainDemo'
        sourceCreatedDateTime = $createdAt
        sourceLastModifiedDateTime = [datetimeoffset]::UtcNow.ToString('o')
        agentIdentityId = $AgentId.ToString()
        agentIdentityBlueprintId = $BlueprintClientId.ToString()
        agentCard = @{
            name = $DisplayName
            version = '1.0.0'
            description = 'Summarizes fictional portfolio DEMO-001 over authenticated MCP.'
            provider = 'Local demonstration'
            capabilities = @{ streaming = $false; pushNotifications = $false }
            defaultInputModes = @('text')
            defaultOutputModes = @('text')
            skills = @(@{
                id = 'portfolio-summary'
                name = 'Portfolio summary'
                description = 'Read-only fictional portfolio summary; no trading or live prices.'
            })
        }
    }
    if (-not $registration.id) { throw 'Registration returned no ID; inspect the registry before retrying.' }
    Write-Host "Agent 365 registration ID: $($registration.id)"
    @{
        tenantId = $TenantId.ToString()
        blueprintClientId = $BlueprintClientId.ToString()
        agentId = $AgentId.ToString()
        registrationId = $registration.id
    } | ConvertTo-Json | Set-Content -Path $StatePath -Encoding utf8
}

if ($GrantObservability) {
    $resourceAppId = '9b975845-388f-4429-889e-eab1ef63949c'
    $resource = Invoke-OnboardingGraph -Path "v1.0/servicePrincipals(appId='$resourceAppId')"
    $roles = @($resource.appRoles | Where-Object {
        $_.value -eq 'Agent365.Observability.OtelWrite' -and $_.isEnabled -and
        $_.allowedMemberTypes -contains 'Application'
    })
    if ($roles.Count -ne 1) {
        throw 'Agent 365 Observability application role is unavailable. Confirm tenant enablement; no delegated fallback is used.'
    }
    $grants = @(Get-OnboardingItems -Path "v1.0/servicePrincipals/$AgentId/appRoleAssignments")
    $existing = @($grants | Where-Object { $_.resourceId -eq $resource.id -and $_.appRoleId -eq $roles[0].id })
    if ($existing.Count -eq 0) {
        $null = Invoke-OnboardingGraph -Method POST -Path "v1.0/servicePrincipals/$($resource.id)/appRoleAssignedTo" -Body @{
            principalId = $AgentId.ToString()
            resourceId = $resource.id
            appRoleId = $roles[0].id
        }
    }
    Write-Host 'Agent365.Observability.OtelWrite application role verified on the existing child identity.'
}

Write-Host 'Open Microsoft 365 admin center > Agents > All agents and locate this registration.'
Write-Host 'Confirm the available Block action for this agent type. Registration alone does not prove local execution can be blocked.'
Write-Host 'No identity was disabled, no secret was rotated, and no Azure hosting resource was created.'
[pscustomobject]@{
    RegistrationId = $registration.id
    AgentId = $AgentId
    BlueprintClientId = $BlueprintClientId
    BlueprintObjectId = $blueprint.id
    PortalUrl = 'https://admin.cloud.microsoft/#/agents/all'
}