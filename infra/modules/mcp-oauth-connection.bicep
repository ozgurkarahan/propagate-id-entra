// ============================================================================
// Module: MCP OAuth Connection (RemoteTool)
// Creates a RemoteTool connection on the Foundry project for MCP OAuth auth.
// The Foundry agent uses this connection to acquire OAuth tokens when calling
// MCP tools via APIM. APIM validates tokens with validate-azure-ad-token policy.
//
// Uses CognitiveServices/accounts/projects/connections@2025-04-01-preview.
// BCP037 warnings for group, connectorName, metadata.type, credentials,
// authorizationUrl, tokenUrl, refreshUrl, scopes are expected and safe to
// ignore — same pattern as apim-mcp.bicep.
//
// IMPORTANT: Connection MUST use category 'RemoteTool' + group 'GenericProtocol'.
// CustomKeys with authType OAuth2 is NOT recognized by Agent Service for OAuth.
// ============================================================================

@description('Name of the Cognitive Services account')
param cognitiveAccountName string

@description('Name of the AI Foundry project (child of cognitive account)')
param projectName string

@description('MCP server endpoint URL (from apim-mcp module)')
param mcpEndpoint string

@description('Foundry OAuth Client app ID')
param clientId string

@secure()
@description('Foundry OAuth Client secret')
param clientSecret string

@description('MCP Gateway Audience app ID (for scope construction)')
param audienceAppId string

resource cognitiveAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: cognitiveAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: cognitiveAccount
  name: projectName
}

resource mcpOAuthConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'mcp-oauth'
  properties: {
    authType: 'OAuth2'
    category: 'RemoteTool'
    group: 'GenericProtocol'
    connectorName: 'mcp-oauth'
    target: mcpEndpoint
    credentials: {
      clientId: clientId
      clientSecret: clientSecret
    }
    authorizationUrl: 'https://login.microsoftonline.com/${tenant().tenantId}/oauth2/v2.0/authorize'
    tokenUrl: 'https://login.microsoftonline.com/${tenant().tenantId}/oauth2/v2.0/token'
    refreshUrl: 'https://login.microsoftonline.com/${tenant().tenantId}/oauth2/v2.0/token'
    scopes: ['api://${audienceAppId}/access_as_user']
    metadata: {
      type: 'custom_MCP'
    }
    isSharedToAll: true
  }
}

@description('Name of the OAuth connection (used by postprovision hook)')
output connectionName string = mcpOAuthConnection.name
