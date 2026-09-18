#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][guid]$TenantId,
    [ValidatePattern('^[A-Za-z0-9-]{3,50}$')][string]$Prefix = 'FinancialAgentLocalDemo',
    [ValidateRange(1, 30)][int]$SecretDays = 7,
    [switch]$NewSecret
)

$ErrorActionPreference = 'Stop'
if (-not $PSCmdlet.ShouldProcess($TenantId, "Provision $Prefix API, blueprint, child identity and app-role assignment")) {
    return
}
if (-not (Get-Module -ListAvailable Microsoft.Graph.Authentication)) {
    throw 'Install-Module Microsoft.Graph.Authentication -Scope CurrentUser before running setup.'
}
Import-Module Microsoft.Graph.Authentication

$scopes = @(
    'User.Read', 'Application.ReadWrite.All', 'AppRoleAssignment.ReadWrite.All',
    'AgentIdentityBlueprint.Create', 'AgentIdentityBlueprint.Read.All',
    'AgentIdentityBlueprint.AddRemoveCreds.All', 'AgentIdentityBlueprintPrincipal.Create',
    'AgentIdentityBlueprintPrincipal.Read.All', 'AgentIdentity.Create.All', 'AgentIdentity.Read.All'
)
Connect-MgGraph -TenantId $TenantId -Scopes $scopes -ContextScope Process -NoWelcome

function Invoke-DemoGraph {
    param([string]$Method = 'GET', [string]$Path, [hashtable]$Body)
    $parameters = @{
        Method = $Method
        Uri = "https://graph.microsoft.com/v1.0/$Path"
        Headers = @{ 'OData-Version' = '4.0' }
        OutputType = 'Hashtable'
    }
    if ($Body) {
        $parameters.Body = $Body | ConvertTo-Json -Depth 15
        $parameters.ContentType = 'application/json'
    }
    Invoke-MgGraphRequest @parameters
}

function Get-DemoItems {
    param([string]$Path)
    $result = Invoke-DemoGraph -Path $Path
    @($result.value)
    while ($result['@odata.nextLink']) {
        $next = [uri]$result['@odata.nextLink']
        if ($next.Scheme -ne 'https' -or $next.Host -ne 'graph.microsoft.com' -or
            -not $next.AbsolutePath.StartsWith('/v1.0/')) {
            throw 'Unexpected Graph pagination URL.'
        }
        $result = Invoke-DemoGraph -Path $next.PathAndQuery.Substring('/v1.0/'.Length)
        @($result.value)
    }
}

function Find-DemoObject {
    param([string]$Collection, [string]$Property, [string]$Value)
    $filter = [uri]::EscapeDataString("$Property eq '$($Value.Replace("'", "''"))'")
    $items = @(Get-DemoItems -Path "${Collection}?`$filter=$filter")
    if ($items.Count -gt 1) {
        throw "Ambiguous $Collection lookup for $Value; use a unique -Prefix."
    }
    if ($items.Count -eq 1) { return $items[0] }
    return $null
}

$owner = Invoke-DemoGraph -Path 'me?$select=id'
$sponsorReference = "https://graph.microsoft.com/v1.0/users/$($owner.id)"
$marker = 'financial-agent-local-demo'
$api = Find-DemoObject -Collection 'applications' -Property 'displayName' -Value "$Prefix-MCP"
if (-not $api) {
    $roleId = [guid]::NewGuid().ToString()
    $api = Invoke-DemoGraph -Method POST -Path 'applications' -Body @{
        displayName = "$Prefix-MCP"
        signInAudience = 'AzureADMyOrg'
        tags = @($marker)
        'owners@odata.bind' = @($sponsorReference)
        api = @{ requestedAccessTokenVersion = 2 }
        optionalClaims = @{ accessToken = @(@{ name = 'idtyp'; essential = $false }) }
        appRoles = @(@{
            id = $roleId
            allowedMemberTypes = @('Application')
            displayName = 'Read demo portfolio'
            description = 'Read fictional portfolio data on the local MCP server'
            value = 'Portfolio.Read'
            isEnabled = $true
        })
    }
}
if ($api.tags -notcontains $marker) {
    throw 'A same-name API exists without the demo tag; use a different -Prefix.'
}
$roles = @($api.appRoles | Where-Object { $_.value -eq 'Portfolio.Read' -and $_.isEnabled })
if ($roles.Count -ne 1 -or $roles[0].allowedMemberTypes -notcontains 'Application' -or
    $api.api.requestedAccessTokenVersion -ne 2 -or
    @($api.optionalClaims.accessToken.name) -notcontains 'idtyp') {
    throw 'Existing demo API configuration has drifted. Restore the v2 token, idtyp and Portfolio.Read configuration.'
}
$roleId = $roles[0].id
$identifierUri = "api://$($api.appId)"
if ($api.identifierUris -notcontains $identifierUri) {
    $null = Invoke-DemoGraph -Method PATCH -Path "applications/$($api.id)" -Body @{
        identifierUris = @($api.identifierUris) + @($identifierUri)
    }
}
$apiPrincipal = Find-DemoObject -Collection 'servicePrincipals' -Property 'appId' -Value $api.appId
if (-not $apiPrincipal) {
    $apiPrincipal = Invoke-DemoGraph -Method POST -Path 'servicePrincipals' -Body @{
        appId = $api.appId
        appRoleAssignmentRequired = $true
    }
}
if (-not $apiPrincipal.appRoleAssignmentRequired) {
    $null = Invoke-DemoGraph -Method PATCH -Path "servicePrincipals/$($apiPrincipal.id)" -Body @{
        appRoleAssignmentRequired = $true
    }
}

$blueprint = Find-DemoObject -Collection 'applications/microsoft.graph.agentIdentityBlueprint' `
    -Property 'displayName' -Value "$Prefix-Blueprint"
if (-not $blueprint) {
    $blueprint = Invoke-DemoGraph -Method POST -Path 'applications/microsoft.graph.agentIdentityBlueprint' -Body @{
        displayName = "$Prefix-Blueprint"
        signInAudience = 'AzureADMyOrg'
        'sponsors@odata.bind' = @($sponsorReference)
        'owners@odata.bind' = @($sponsorReference)
    }
}
$blueprintPrincipal = Find-DemoObject -Collection 'servicePrincipals' -Property 'appId' -Value $blueprint.appId
if (-not $blueprintPrincipal) {
    $blueprintPrincipal = Invoke-DemoGraph -Method POST `
        -Path 'servicePrincipals/microsoft.graph.agentIdentityBlueprintPrincipal' -Body @{ appId = $blueprint.appId }
}
$typedPrincipal = Invoke-DemoGraph -Path "servicePrincipals/$($blueprintPrincipal.id)/microsoft.graph.agentIdentityBlueprintPrincipal"
if (-not $typedPrincipal.id) { throw 'The blueprint principal is not an Agent ID blueprint principal.' }

$agent = Find-DemoObject -Collection 'servicePrincipals/microsoft.graph.agentIdentity' `
    -Property 'displayName' -Value "$Prefix-Agent"
if (-not $agent) {
    $agent = Invoke-DemoGraph -Method POST -Path 'servicePrincipals/microsoft.graph.agentIdentity' -Body @{
        displayName = "$Prefix-Agent"
        agentIdentityBlueprintId = $blueprint.appId
        'sponsors@odata.bind' = @($sponsorReference)
        'owners@odata.bind' = @($sponsorReference)
    }
}
if ($agent.agentIdentityBlueprintId -ne $blueprint.appId) {
    throw 'The existing agent belongs to a different blueprint; use a unique -Prefix.'
}
$assignments = @(Get-DemoItems -Path "servicePrincipals/$($agent.id)/appRoleAssignments")
$assignment = @($assignments | Where-Object {
    $_.resourceId -eq $apiPrincipal.id -and $_.appRoleId -eq $roleId
})
if ($assignment.Count -eq 0) {
    $null = Invoke-DemoGraph -Method POST -Path "servicePrincipals/$($apiPrincipal.id)/appRoleAssignedTo" -Body @{
        principalId = $agent.id
        resourceId = $apiPrincipal.id
        appRoleId = $roleId
    }
}

$currentBlueprint = Invoke-DemoGraph -Path "applications/$($blueprint.id)/microsoft.graph.agentIdentityBlueprint"
$existingSecrets = @($currentBlueprint.passwordCredentials | Where-Object {
    $_.displayName -eq "$Prefix-LocalSecret" -and [datetimeoffset]$_.endDateTime -gt [datetimeoffset]::UtcNow
})
$secureSecret = $null
if ($NewSecret -or $existingSecrets.Count -eq 0) {
    $password = Invoke-DemoGraph -Method POST `
        -Path "applications/$($blueprint.id)/microsoft.graph.agentIdentityBlueprint/addPassword" -Body @{
        passwordCredential = @{
            displayName = "$Prefix-LocalSecret"
            endDateTime = [datetimeoffset]::UtcNow.AddDays($SecretDays).ToString('o')
        }
    }
    $secureSecret = ConvertTo-SecureString $password.secretText -AsPlainText -Force
    $env:ENTRA_BLUEPRINT_CLIENT_SECRET = $password.secretText
    $password = $null
    Write-Host 'A new blueprint secret is set in this PowerShell process only. It is not printed or written to disk.'
} else {
    if ($env:ENTRA_BLUEPRINT_CLIENT_ID -ne $blueprint.appId) {
        Remove-Item Env:ENTRA_BLUEPRINT_CLIENT_SECRET -ErrorAction SilentlyContinue
    }
    Write-Host 'Reusing the existing blueprint credential. Supply its secret locally, or use -NewSecret to add a replacement.'
}
$env:ENTRA_TENANT_ID = $TenantId.ToString()
$env:ENTRA_BLUEPRINT_CLIENT_ID = $blueprint.appId
$env:ENTRA_AGENT_ID = $agent.id
$env:MCP_API_CLIENT_ID = $api.appId
Write-Host "ENTRA_TENANT_ID=$env:ENTRA_TENANT_ID"
Write-Host "ENTRA_BLUEPRINT_CLIENT_ID=$env:ENTRA_BLUEPRINT_CLIENT_ID"
Write-Host "ENTRA_AGENT_ID=$env:ENTRA_AGENT_ID"
Write-Host "MCP_API_CLIENT_ID=$env:MCP_API_CLIENT_ID"
Write-Host 'Setup complete. Run the agent from this shell; configure the server shell with the nonsecret IDs above.'

[pscustomobject]@{
    TenantId = $TenantId
    ApiApplicationObjectId = $api.id
    ApiClientId = $api.appId
    ApiServicePrincipalId = $apiPrincipal.id
    BlueprintApplicationObjectId = $blueprint.id
    BlueprintClientId = $blueprint.appId
    BlueprintPrincipalId = $blueprintPrincipal.id
    AgentId = $agent.id
    RoleId = $roleId
    ClientSecret = $secureSecret
}