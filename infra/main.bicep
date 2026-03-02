targetScope = 'subscription'

@description('Name of the azd environment')
@minLength(1)
@maxLength(64)
param environmentName string

@description('Azure region for all resources')
param location string = 'swedencentral'

@description('Suffix for the CognitiveServices account name (increment after azd down --purge)')
param cognitiveAccountSuffix string = ''

var baseName = toLower(environmentName)
var resourceToken = toLower(uniqueString(subscription().id, baseName, location))
var tags = {
  'azd-env-name': environmentName
  project: baseName
}

// --- Resource Group ---
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${baseName}'
  location: location
  tags: tags
}

// ============================================================
// Tier 1: Independent modules (deploy in parallel)
// ============================================================

module monitoring 'modules/monitoring.bicep' = {
  name: 'monitoring'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    resourceToken: resourceToken
  }
}

module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
  }
}

module registry 'modules/registry.bicep' = {
  name: 'registry'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    resourceToken: resourceToken
  }
}

// cognitive module now includes: AI Services account + Project + Connection + gpt-4o
module cognitive 'modules/cognitive.bicep' = {
  name: 'cognitive'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    accountSuffix: cognitiveAccountSuffix
  }
}

// Entra ID App Registrations
// Created by postprovision hook (az CLI with delegated permissions) — the Graph
// Bicep extension requires Application.ReadWrite.All on the ARM deployment
// identity, which is not available in managed tenants.

// ============================================================
// Tier 1.5: Modules with Tier 1 dependencies (monitoring only)
// ============================================================

module workbook 'modules/workbook.bicep' = {
  name: 'workbook'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    appInsightsId: monitoring.outputs.appInsightsId
  }
}

// ============================================================
// Tier 2: Modules with Tier 1 dependencies
// ============================================================

module containerApp 'modules/container-app.bicep' = {
  name: 'container-app'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    registryLoginServer: registry.outputs.registryLoginServer
    registryName: registry.outputs.registryName
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

// ============================================================
// Tier 2.5: Chat App (needs registry + container environment + cognitive)
// ============================================================

module chatApp 'modules/chat-app.bicep' = {
  name: 'chat-app'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    registryLoginServer: registry.outputs.registryLoginServer
    registryName: registry.outputs.registryName
    containerAppsEnvironmentId: containerApp.outputs.containerAppsEnvironmentId
    projectEndpoint: cognitive.outputs.projectEndpoint
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
  }
}

// ============================================================
// Tier 3: Modules with Tier 2 dependencies
// ============================================================

module apim 'modules/apim.bicep' = {
  name: 'apim'
  scope: rg
  params: {
    name: baseName
    location: location
    tags: tags
    ordersApiFqdn: containerApp.outputs.containerAppFqdn
    cognitiveEndpoint: cognitive.outputs.openaiEndpoint
    appInsightsConnectionString: monitoring.outputs.appInsightsConnectionString
    logAnalyticsWorkspaceId: monitoring.outputs.logAnalyticsWorkspaceId
  }
}

// ============================================================
// Tier 3.5: MCP Server (depends on APIM + its REST API operations)
// ============================================================

module apimMcp 'modules/apim-mcp.bicep' = {
  name: 'apim-mcp'
  scope: rg
  params: {
    apimName: apim.outputs.apimName
    ordersApiUrl: 'https://${containerApp.outputs.containerAppFqdn}'
  }
}

// ============================================================
// Tier 4: Modules with Tier 3 dependencies
// ============================================================

module apimCognitiveRoleAssignment 'modules/role-assignment.bicep' = {
  name: 'apim-cognitive-role'
  scope: rg
  params: {
    cognitiveAccountName: cognitive.outputs.cognitiveAccountName
    principalId: apim.outputs.apimPrincipalId
  }
}

module aiGatewayConnection 'modules/ai-gateway-connection.bicep' = {
  name: 'ai-gateway-connection'
  scope: rg
  params: {
    cognitiveAccountName: cognitive.outputs.cognitiveAccountName
    projectName: cognitive.outputs.projectName
    apimGatewayUrl: apim.outputs.apimGatewayUrl
    apimResourceId: apim.outputs.apimResourceId
  }
}

module chatAppCognitiveRole 'modules/role-assignment.bicep' = {
  name: 'chat-app-cognitive-role'
  scope: rg
  params: {
    cognitiveAccountName: cognitive.outputs.cognitiveAccountName
    principalId: chatApp.outputs.chatAppPrincipalId
  }
}

module chatAppCognitiveContributor 'modules/role-assignment.bicep' = {
  name: 'chat-app-cognitive-contributor'
  scope: rg
  params: {
    cognitiveAccountName: cognitive.outputs.cognitiveAccountName
    principalId: chatApp.outputs.chatAppPrincipalId
    roleDefinitionId: '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68' // Cognitive Services Contributor
  }
}

module mcpOAuthConnection 'modules/mcp-oauth-connection.bicep' = {
  name: 'mcp-oauth-connection'
  scope: rg
  params: {
    cognitiveAccountName: cognitive.outputs.cognitiveAccountName
    projectName: cognitive.outputs.projectName
    mcpEndpoint: apimMcp.outputs.mcpEndpoint
  }
}

// ============================================================
// Outputs (become azd env vars)
// ============================================================

output AZURE_CONTAINER_REGISTRY_NAME string = registry.outputs.registryName
output AZURE_CONTAINER_REGISTRY_ENDPOINT string = registry.outputs.registryLoginServer
output ORDERS_API_URL string = 'https://${containerApp.outputs.containerAppFqdn}'
output ORDERS_API_CONTAINER_APP_NAME string = containerApp.outputs.containerAppName
output APIM_GATEWAY_URL string = apim.outputs.apimGatewayUrl
output APIM_NAME string = apim.outputs.apimName
output APIM_ORDERS_API_PATH string = apim.outputs.ordersApiPath
output AI_FOUNDRY_PROJECT_NAME string = cognitive.outputs.projectName
output AI_FOUNDRY_PROJECT_ENDPOINT string = cognitive.outputs.projectEndpoint
output APIM_OPENAI_ENDPOINT string = '${apim.outputs.apimGatewayUrl}/openai'
output COGNITIVE_ACCOUNT_NAME string = cognitive.outputs.cognitiveAccountName
output AZURE_RESOURCE_GROUP string = rg.name
output APIM_MCP_ENDPOINT string = apimMcp.outputs.mcpEndpoint
output MCP_CONNECTION_NAME string = mcpOAuthConnection.outputs.connectionName
output CHAT_APP_URL string = 'https://${chatApp.outputs.chatAppFqdn}'
output CHAT_APP_FQDN string = chatApp.outputs.chatAppFqdn
output CHAT_APP_CONTAINER_APP_NAME string = chatApp.outputs.chatAppName
// CHAT_APP_ENTRA_CLIENT_ID set by postprovision hook
// (Entra app created via az CLI, not Bicep)
