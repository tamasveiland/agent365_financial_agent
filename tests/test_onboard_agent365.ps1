#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$store = @{ registrations = @(); grants = @(); connections = 0; requests = @() }
$tenant = '11111111-1111-1111-1111-111111111111'
$testBlueprintId = '22222222-2222-2222-2222-222222222222'
$testAgentId = '33333333-3333-3333-3333-333333333333'
$statePath = Join-Path ([IO.Path]::GetTempPath()) "agent365-test-$([guid]::NewGuid()).json"

function Get-Module { param($ListAvailable) return @{ Name = 'Microsoft.Graph.Authentication' } }
function Import-Module { param($Name) }
function Connect-MgGraph { param($TenantId, $Scopes, $ContextScope, [switch]$NoWelcome) $store.connections++ }
function Invoke-MgGraphRequest {
    param($Method, $Uri, $Headers, $OutputType, $Body, $ContentType)
    $path = ([uri]$Uri).AbsolutePath
    $store.requests += "$Method $path"
    if ($path -match '/applications\(appId=') {
        return @{ id = 'blueprint-object'; appId = $testBlueprintId }
    }
    if ($path.EndsWith('/microsoft.graph.agentIdentity')) {
        return @{ id = $testAgentId; agentIdentityBlueprintId = $testBlueprintId; createdDateTime = '2026-09-18T00:00:00Z' }
    }
    if ($path -eq '/v1.0/me') { return @{ id = 'admin-owner' } }
    if ($Method -eq 'POST' -and $path -eq '/beta/copilot/agentRegistrations') {
        $payload = $Body | ConvertFrom-Json -AsHashtable
        $payload.id = 'registered-agent'
        $store.registrations += $payload
        return $payload
    }
    if ($Method -eq 'GET' -and $path -eq '/beta/copilot/agentRegistrations/registered-agent') {
        return $store.registrations[0]
    }
    if ($path.StartsWith('/v1.0/servicePrincipals(appId=')) {
        return @{ id = 'observability-resource'; appRoles = @(@{
            id = 'otel-role'; value = 'Agent365.Observability.OtelWrite';
            isEnabled = $true; allowedMemberTypes = @('Application')
        }) }
    }
    if ($path.EndsWith('/appRoleAssignments')) { return @{ value = $store.grants } }
    if ($Method -eq 'POST' -and $path.EndsWith('/appRoleAssignedTo')) {
        $store.grants += ($Body | ConvertFrom-Json -AsHashtable)
        return @{}
    }
    throw "Unexpected Graph operation $Method $path"
}
function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

try {
    $scriptPath = Join-Path $PSScriptRoot '../scripts/onboard_agent365.ps1'
    $parameters = @{ TenantId = $tenant; BlueprintClientId = $testBlueprintId; AgentId = $testAgentId;
        StatePath = $statePath; GrantObservability = $true }
    $null = & $scriptPath @parameters -WhatIf
    Assert-True ($store.connections -eq 0) 'WhatIf must not authenticate'
    $first = & $scriptPath @parameters
    Assert-True ($store.registrations.Count -eq 1) 'Expected one registration'
    Assert-True ($store.registrations[0].agentIdentityId -eq $testAgentId) 'Must reuse child identity'
    Assert-True ($store.registrations[0].agentIdentityBlueprintId -eq $testBlueprintId) 'Must reuse blueprint'
    Assert-True ($store.grants[0].principalId -eq $testAgentId) 'Grant must target child, not blueprint'
    Assert-True ($store.grants[0].resourceId -eq 'observability-resource') 'Wrong permission resource'
    $second = & $scriptPath @parameters
    Assert-True ($store.registrations.Count -eq 1 -and $store.grants.Count -eq 1) 'Rerun must not duplicate objects or grants'
    Assert-True ($first.RegistrationId -eq $second.RegistrationId) 'Rerun changed registration'
    Assert-True (-not ($store.requests -match 'addPassword|accountEnabled|/applications$')) 'Onboarding must not rotate credentials or create replacement apps'
    $parameters.TenantId = '55555555-5555-5555-5555-555555555555'
    $rejected = $false
    try { $null = & $scriptPath @parameters } catch { $rejected = $_.Exception.Message -match 'another identity' }
    Assert-True $rejected 'Cross-tenant registration state must be rejected'
    Write-Output 'Agent 365 onboarding tests: identity reuse, role target, idempotency, WhatIf and state isolation passed.'
} finally {
    Remove-Item $statePath -ErrorAction SilentlyContinue
}