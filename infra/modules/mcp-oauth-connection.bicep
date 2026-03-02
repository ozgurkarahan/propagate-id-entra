// ============================================================================
// Module: MCP UserEntraToken Connection (RemoteTool)
// Creates a RemoteTool connection on the Foundry project that passes the
// user's existing Entra token (aud=https://ai.azure.com) directly to APIM.
// No OAuth2 client flow, no consent, no refresh tokens.
//
// Uses CognitiveServices/accounts/projects/connections@2025-04-01-preview.
// BCP037 warnings for metadata.type are expected and safe to ignore —
// same pattern as apim-mcp.bicep.
// ============================================================================

@description('Name of the Cognitive Services account')
param cognitiveAccountName string

@description('Name of the AI Foundry project (child of cognitive account)')
param projectName string

@description('MCP server endpoint URL (from apim-mcp module)')
param mcpEndpoint string

resource cognitiveAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: cognitiveAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: cognitiveAccount
  name: projectName
}

resource mcpEntraConnection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-04-01-preview' = {
  parent: project
  name: 'mcp-entra'
  properties: {
    authType: 'UserEntraToken'
    category: 'RemoteTool'
    target: mcpEndpoint
    audience: 'https://ai.azure.com'
    metadata: {
      type: 'custom_MCP'
      audience: 'https://ai.azure.com'
    }
    isSharedToAll: true
  }
}

@description('Name of the connection (used by postprovision hook)')
output connectionName string = mcpEntraConnection.name
