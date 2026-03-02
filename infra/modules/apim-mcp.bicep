// ============================================================================
// Module: APIM MCP Server
// Exposes the Orders REST API as an MCP server using APIM's native MCP feature.
// APIM converts existing REST API operations into MCP tools automatically.
//
// API versions: 2025-03-01-preview for MCP resources (apiType/mcpTools),
// 2024-06-01-preview for existing resource references (matching apim.bicep).
// Bicep shows BCP037 warnings for MCP properties — expected and safe to ignore.
// ============================================================================

@description('Name of the existing API Management instance')
param apimName string

@description('Backend URL for the Orders API (Container App)')
param ordersApiUrl string

// --------------------------------------------------------------------------
// Operations to expose as MCP tools (must match operation names in apim.bicep)
// --------------------------------------------------------------------------
var mcpOperationNames = [
  'list-orders'
  'get-order'
  'create-order'
  'update-order'
  'delete-order'
  'health-check'
]

// --------------------------------------------------------------------------
// Reference existing APIM instance, REST API, and operations
// --------------------------------------------------------------------------
resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' existing = {
  name: apimName
}

resource ordersApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' existing = {
  parent: apim
  name: 'orders-api'
}

resource operations 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' existing = [
  for op in mcpOperationNames: {
    parent: ordersApi
    name: op
  }
]

// --------------------------------------------------------------------------
// MCP API Resource
// Routes tool calls directly to the Container App backend.
// --------------------------------------------------------------------------
resource mcpApi 'Microsoft.ApiManagement/service/apis@2025-03-01-preview' = {
  parent: apim
  name: 'orders-mcp'
  properties: {
    displayName: 'Orders MCP Server'
    description: 'MCP server exposing Orders API operations as tools for AI agents.'
    path: 'orders-mcp'
    protocols: [
      'https'
    ]
    serviceUrl: ordersApiUrl
    subscriptionRequired: false
    apiType: 'mcp'
    type: 'mcp'
    mcpTools: [
      for (op, i) in mcpOperationNames: {
        name: operations[i].name
        operationId: operations[i].id
      }
    ]
  }
}

// --------------------------------------------------------------------------
// APIM Named Values (used by policy XML templates)
// --------------------------------------------------------------------------
resource mcpTenantIdNV 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'McpTenantId'
  properties: {
    displayName: 'McpTenantId'
    value: tenant().tenantId
    secret: false
  }
}

resource apimGatewayUrlNV 'Microsoft.ApiManagement/service/namedValues@2024-06-01-preview' = {
  parent: apim
  name: 'APIMGatewayURL'
  properties: {
    displayName: 'APIMGatewayURL'
    value: apim.properties.gatewayUrl
    secret: false
  }
}

// --------------------------------------------------------------------------
// MCP API-level policy (validate-azure-ad-token + WWW-Authenticate 401)
// --------------------------------------------------------------------------
resource mcpApiPolicy 'Microsoft.ApiManagement/service/apis/policies@2024-06-01-preview' = {
  parent: mcpApi
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/mcp-api-policy.xml')
  }
  dependsOn: [ mcpTenantIdNV, apimGatewayUrlNV ]
}

// --------------------------------------------------------------------------
// PRM endpoint (RFC 9728 Protected Resource Metadata — anonymous access)
// MCP-type APIs don't support custom operations, so PRM lives on a separate
// HTTP API. APIM longest-prefix routing ensures requests to
// /orders-mcp/.well-known/* hit this API, not the MCP API.
// --------------------------------------------------------------------------
resource prmApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'orders-mcp-prm'
  properties: {
    displayName: 'MCP Protected Resource Metadata'
    path: 'orders-mcp/.well-known'
    protocols: [
      'https'
    ]
    subscriptionRequired: false
    apiType: 'http'
  }
}

resource prmOp 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: prmApi
  name: 'oauth-protected-resource'
  properties: {
    displayName: 'Protected Resource Metadata'
    method: 'GET'
    urlTemplate: '/oauth-protected-resource'
  }
}

resource prmOpPolicy 'Microsoft.ApiManagement/service/apis/operations/policies@2024-06-01-preview' = {
  parent: prmOp
  name: 'policy'
  properties: {
    format: 'rawxml'
    value: loadTextContent('../policies/mcp-prm-policy.xml')
  }
}

// --------------------------------------------------------------------------
// Outputs
// --------------------------------------------------------------------------
@description('MCP endpoint URL')
output mcpEndpoint string = '${apim.properties.gatewayUrl}/orders-mcp/mcp'
