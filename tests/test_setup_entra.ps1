#Requires -Version 7.0
$ErrorActionPreference = 'Stop'
$environmentNames = @('ENTRA_TENANT_ID', 'ENTRA_BLUEPRINT_CLIENT_ID', 'ENTRA_AGENT_ID',
    'MCP_API_CLIENT_ID', 'ENTRA_BLUEPRINT_CLIENT_SECRET')
$savedEnvironment = @{}
foreach ($name in $environmentNames) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
}
$store = @{
    applications = @()
    servicePrincipals = @()
    assignments = @()
    passwords = @()
    writes = 0
    connections = 0
}

function Get-Module { param($ListAvailable) return @{ Name = 'Microsoft.Graph.Authentication' } }
function Import-Module { param($Name) }
function Connect-MgGraph { param($TenantId, $Scopes, $ContextScope, [switch]$NoWelcome) $store.connections++ }
function Invoke-MgGraphRequest {
    param($Method, $Uri, $Headers, $OutputType, $Body, $ContentType)
    if ($Headers['OData-Version'] -ne '4.0') { throw 'Missing OData header' }
    $uriObject = [uri]$Uri
    $path = $uriObject.AbsolutePath.Substring('/v1.0/'.Length)
    $payload = if ($Body) { $Body | ConvertFrom-Json -AsHashtable } else { @{} }
    if ($Method -eq 'GET' -and $path -eq 'me') { return @{ id = 'owner-id' } }
    if ($Method -eq 'GET' -and $uriObject.Query -match 'filter=') {
        $collection = if ($path.StartsWith('applications')) { 'applications' } else { 'servicePrincipals' }
        $filter = [uri]::UnescapeDataString($uriObject.Query.Split('=', 2)[1])
        if ($filter -notmatch "^(?<property>\w+) eq '(?<value>[^']+)'$") { throw 'Unexpected filter' }
        $property = $Matches.property
        $value = $Matches.value
        return @{ value = @($store[$collection] | Where-Object { $_[$property] -eq $value }) }
    }
    if ($Method -eq 'GET' -and $path.EndsWith('/appRoleAssignments')) {
        return @{ value = $store.assignments }
    }
    if ($Method -eq 'GET') {
        $parts = $path.Split('/')
        $item = @($store[$parts[0]] | Where-Object { $_.id -eq $parts[1] })
        if ($item.Count -ne 1) { throw "Missing object $path" }
        return $item[0]
    }
    $store.writes++
    if ($Method -eq 'POST' -and $path.EndsWith('/appRoleAssignedTo')) {
        $store.assignments += $payload
        return $payload
    }
    if ($Method -eq 'POST' -and $path.EndsWith('/addPassword')) {
        $blueprintId = $path.Split('/')[1]
        $blueprint = $store.applications | Where-Object { $_.id -eq $blueprintId }
        $secret = $payload.passwordCredential.Clone()
        $secret.keyId = [guid]::NewGuid().ToString()
        $blueprint.passwordCredentials += $secret
        $store.passwords += $secret
        $secret = $secret.Clone()
        $secret.secretText = 'mock-only-secret-do-not-print'
        return $secret
    }
    if ($Method -eq 'PATCH') {
        $parts = $path.Split('/')
        $item = $store[$parts[0]] | Where-Object { $_.id -eq $parts[1] }
        foreach ($key in $payload.Keys) { $item[$key] = $payload[$key] }
        return
    }
    if ($Method -eq 'POST') {
        $collection = $path.Split('/')[0]
        $payload.id = [guid]::NewGuid().ToString()
        if ($collection -eq 'applications') {
            $payload.appId = [guid]::NewGuid().ToString()
            $payload.identifierUris = @()
            $payload.passwordCredentials = @()
        }
        $store[$collection] += $payload
        return $payload
    }
    throw "Unhandled request: $Method $path"
}

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

try {
    $setupScript = Join-Path $PSScriptRoot '../scripts/setup_entra.ps1'
    $tenant = '11111111-1111-1111-1111-111111111111'
    $null = & $setupScript -TenantId $tenant -WhatIf
    Assert-True ($script:store.connections -eq 0) 'WhatIf must not connect to Graph'
    $first = & $setupScript -TenantId $tenant
    Assert-True ($script:store.applications.Count -eq 2) 'Expected API and blueprint applications'
    Assert-True ($script:store.servicePrincipals.Count -eq 3) 'Expected API, blueprint and child principals'
    Assert-True ($script:store.assignments.Count -eq 1) 'Expected one role assignment'
    Assert-True ($script:store.assignments[0].principalId -eq $first.AgentId) 'Role must be assigned to child identity'
    Assert-True ($script:store.assignments[0].resourceId -eq $first.ApiServicePrincipalId) 'Wrong API principal'
    Assert-True ($first.ClientSecret -is [securestring]) 'Secret must not be returned in plaintext'
    $writes = $script:store.writes
    $second = & $setupScript -TenantId $tenant
    Assert-True ($script:store.writes -eq $writes) 'Rerun must not mutate resources or issue another secret'
    Assert-True ($first.AgentId -eq $second.AgentId) 'Rerun must reuse child identity'
    $null = & $setupScript -TenantId $tenant -NewSecret
    Assert-True ($script:store.passwords.Count -eq 2) 'Explicit rotation should issue exactly one new secret'
    Write-Output 'Provisioning tests: first setup, child role assignment, idempotent rerun, rotation and WhatIf passed.'
} finally {
    foreach ($name in $environmentNames) {
        [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name])
    }
}