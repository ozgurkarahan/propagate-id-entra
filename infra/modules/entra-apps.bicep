// ======================================================================
// NOT USED — kept for reference only.
//
// This module requires Application.ReadWrite.All on the ARM deployment
// identity, which is unavailable in managed tenants. Entra apps are
// created by hooks/postprovision.py using az CLI with delegated
// permissions instead.
// ======================================================================

// ============================================================================
// Module: Entra ID App Registrations (Microsoft.Graph Bicep Extension)
//
// Creates two app registrations for MCP OAuth:
//   1. MCP Gateway Audience — with access_as_user scope
//   2. Foundry OAuth Client — with redirect URI, public client, API permission
//
// Also creates service principals and admin consent grant.
//
// Limitations handled by postprovision hook:
//   - identifierUris: can't self-reference appId in Bicep (circular dependency)
//   - passwordCredentials: secretText is read-only in Graph API
//
// Uses Microsoft.Graph Bicep extension (GA since July 2025).
// Requires bicepconfig.json with extension source configured.
// ============================================================================

extension microsoftGraph

// Deterministic scope ID — stable across re-deployments
var scopeId = guid('mcp-access-as-user', tenant().tenantId)

// --- MCP Gateway Audience App ---
// Exposes the access_as_user scope that the Foundry OAuth Client requests.
// identifierUris (api://{appId}) is set by postprovision hook after creation.
resource audienceApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: 'mcp-gateway-audience'
  displayName: 'MCP Gateway Audience'
  signInAudience: 'AzureADMyOrg'
  api: {
    oauth2PermissionScopes: [
      {
        id: scopeId
        adminConsentDescription: 'Access MCP Gateway as user'
        adminConsentDisplayName: 'Access MCP Gateway as user'
        isEnabled: true
        type: 'User'
        userConsentDescription: 'Access MCP Gateway as user'
        userConsentDisplayName: 'Access MCP Gateway as user'
        value: 'access_as_user'
      }
    ]
  }
}

resource audienceSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: audienceApp.appId
}

// --- Foundry OAuth Client App ---
// Used by the Foundry agent to acquire OAuth tokens for MCP tool calls.
// Client secret is created by postprovision hook (Graph API doesn't allow
// setting secretText declaratively).
resource clientApp 'Microsoft.Graph/applications@v1.0' = {
  uniqueName: 'foundry-oauth-client'
  displayName: 'Foundry OAuth Client'
  signInAudience: 'AzureADMyOrg'
  isFallbackPublicClient: true
  web: {
    redirectUris: [
      'https://ai.azure.com/auth/callback'
    ]
  }
  requiredResourceAccess: [
    {
      resourceAppId: audienceApp.appId
      resourceAccess: [
        {
          id: scopeId
          type: 'Scope'
        }
      ]
    }
  ]
}

resource clientSp 'Microsoft.Graph/servicePrincipals@v1.0' = {
  appId: clientApp.appId
}

// --- Admin Consent Grant ---
// Grants all principals access to the access_as_user scope on the audience app.
resource adminConsent 'Microsoft.Graph/oauth2PermissionGrants@v1.0' = {
  clientId: clientSp.id
  consentType: 'AllPrincipals'
  resourceId: audienceSp.id
  scope: 'access_as_user'
}

@description('Application (client) ID of the MCP Gateway Audience app')
output audienceAppId string = audienceApp.appId

@description('Application (client) ID of the Foundry OAuth Client app')
output clientAppId string = clientApp.appId
