@description('Base name for resources')
param name string

@description('Azure region')
param location string

@description('Resource tags')
param tags object = {}

@description('Orders API FQDN (from Container App)')
param ordersApiFqdn string

@description('Azure OpenAI endpoint URL (from cognitive module)')
param cognitiveEndpoint string

@description('Application Insights connection string for APIM diagnostics')
param appInsightsConnectionString string

@description('Log Analytics workspace resource ID for diagnostic settings')
param logAnalyticsWorkspaceId string

var backendUrl = 'https://${ordersApiFqdn}'
var openaiBackendUrl = '${cognitiveEndpoint}openai'

// --- APIM Instance ---
resource apim 'Microsoft.ApiManagement/service@2024-06-01-preview' = {
  name: 'apim-${name}'
  location: location
  tags: tags
  sku: {
    name: 'StandardV2'
    capacity: 1
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    publisherEmail: 'admin@identity-poc.dev'
    publisherName: 'Identity PoC'
  }
}

// --- Backend ---
resource backend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  parent: apim
  name: 'orders-api-backend'
  properties: {
    url: backendUrl
    protocol: 'http'
    title: 'Orders API'
  }
}

// --- Orders REST API ---
resource ordersApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'orders-api'
  properties: {
    displayName: 'Orders API'
    path: 'orders-api'
    protocols: ['https']
    serviceUrl: backendUrl
    subscriptionRequired: false
    apiType: 'http'
  }
}

// --- API Schema (request body definitions for MCP tool generation) ---
resource ordersSchema 'Microsoft.ApiManagement/service/apis/schemas@2024-06-01-preview' = {
  parent: ordersApi
  name: 'orders-schema'
  properties: {
    contentType: 'application/vnd.oai.openapi.components+json'
    document: {
      components: {
        schemas: {
          CreateOrderRequest: {
            type: 'object'
            required: [
              'customer_name'
              'product'
              'quantity'
            ]
            properties: {
              customer_name: {
                type: 'string'
                description: 'Full name of the customer'
              }
              product: {
                type: 'string'
                description: 'Product name'
              }
              quantity: {
                type: 'integer'
                description: 'Number of items to order'
              }
            }
          }
          UpdateOrderRequest: {
            type: 'object'
            properties: {
              customer_name: {
                type: 'string'
                description: 'Full name of the customer'
              }
              product: {
                type: 'string'
                description: 'Product name'
              }
              quantity: {
                type: 'integer'
                description: 'Number of items'
              }
              status: {
                type: 'string'
                description: 'Order status (pending, shipped, delivered)'
                enum: [
                  'pending'
                  'shipped'
                  'delivered'
                ]
              }
            }
          }
        }
      }
    }
  }
}

// --- REST API Operations ---
resource opListOrders 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'list-orders'
  properties: {
    displayName: 'List Orders'
    method: 'GET'
    urlTemplate: '/orders'
    description: 'Get all orders'
  }
}

resource opGetOrder 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'get-order'
  properties: {
    displayName: 'Get Order'
    method: 'GET'
    urlTemplate: '/orders/{order_id}'
    description: 'Get a specific order by ID'
    templateParameters: [
      {
        name: 'order_id'
        required: true
        type: 'string'
        description: 'Order ID (e.g. ORD-001)'
      }
    ]
  }
}

resource opCreateOrder 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'create-order'
  dependsOn: [ordersSchema]
  properties: {
    displayName: 'Create Order'
    method: 'POST'
    urlTemplate: '/orders'
    description: 'Create a new order with customer_name, product, and quantity'
    request: {
      representations: [
        {
          contentType: 'application/json'
          schemaId: 'orders-schema'
          typeName: 'CreateOrderRequest'
        }
      ]
    }
    responses: [
      {
        statusCode: 201
        description: 'Order created successfully'
      }
    ]
  }
}

resource opUpdateOrder 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'update-order'
  dependsOn: [ordersSchema]
  properties: {
    displayName: 'Update Order'
    method: 'PUT'
    urlTemplate: '/orders/{order_id}'
    description: 'Update an existing order. All fields are optional — only provided fields are updated.'
    templateParameters: [
      {
        name: 'order_id'
        required: true
        type: 'string'
        description: 'Order ID (e.g. ORD-001)'
      }
    ]
    request: {
      representations: [
        {
          contentType: 'application/json'
          schemaId: 'orders-schema'
          typeName: 'UpdateOrderRequest'
        }
      ]
    }
    responses: [
      {
        statusCode: 200
        description: 'Order updated successfully'
      }
    ]
  }
}

resource opDeleteOrder 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'delete-order'
  properties: {
    displayName: 'Delete Order'
    method: 'DELETE'
    urlTemplate: '/orders/{order_id}'
    description: 'Delete an order'
    templateParameters: [
      {
        name: 'order_id'
        required: true
        type: 'string'
        description: 'Order ID (e.g. ORD-001)'
      }
    ]
  }
}

resource opHealth 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: ordersApi
  name: 'health-check'
  properties: {
    displayName: 'Health Check'
    method: 'GET'
    urlTemplate: '/health'
    description: 'Health check endpoint'
  }
}

// --- OpenAI Backend ---
resource openaiBackend 'Microsoft.ApiManagement/service/backends@2024-06-01-preview' = {
  parent: apim
  name: 'openai-backend'
  properties: {
    url: openaiBackendUrl
    protocol: 'http'
    title: 'Azure OpenAI'
  }
}

// --- Azure OpenAI API (AI Gateway) ---
resource openaiApi 'Microsoft.ApiManagement/service/apis@2024-06-01-preview' = {
  parent: apim
  name: 'azure-openai'
  properties: {
    displayName: 'Azure OpenAI'
    path: 'openai'
    protocols: ['https']
    serviceUrl: openaiBackendUrl
    subscriptionRequired: false
    apiType: 'http'
    format: 'rawxml'
    value: loadTextContent('../policies/ai-gateway-policy.xml')
  }
}

resource opChatCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: openaiApi
  name: 'chat-completions'
  properties: {
    displayName: 'Chat Completions'
    method: 'POST'
    urlTemplate: '/deployments/{deployment-id}/chat/completions'
    description: 'Creates a completion for the chat message'
    templateParameters: [
      {
        name: 'deployment-id'
        required: true
        type: 'string'
        description: 'Deployment ID (e.g. gpt-4o)'
      }
    ]
  }
}

resource opCompletions 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: openaiApi
  name: 'completions'
  properties: {
    displayName: 'Completions'
    method: 'POST'
    urlTemplate: '/deployments/{deployment-id}/completions'
    description: 'Creates a completion for the provided prompt'
    templateParameters: [
      {
        name: 'deployment-id'
        required: true
        type: 'string'
        description: 'Deployment ID (e.g. gpt-4o)'
      }
    ]
  }
}

resource opEmbeddings 'Microsoft.ApiManagement/service/apis/operations@2024-06-01-preview' = {
  parent: openaiApi
  name: 'embeddings'
  properties: {
    displayName: 'Embeddings'
    method: 'POST'
    urlTemplate: '/deployments/{deployment-id}/embeddings'
    description: 'Creates an embedding vector for the input'
    templateParameters: [
      {
        name: 'deployment-id'
        required: true
        type: 'string'
        description: 'Deployment ID (e.g. text-embedding-ada-002)'
      }
    ]
  }
}

// --- APIM Logger (Application Insights) ---
resource apimLogger 'Microsoft.ApiManagement/service/loggers@2024-06-01-preview' = {
  parent: apim
  name: 'appinsights-logger'
  properties: {
    loggerType: 'applicationInsights'
    credentials: {
      connectionString: appInsightsConnectionString
    }
  }
}

// --- APIM Diagnostics (log request/response headers for all APIs) ---
// IMPORTANT: Response body bytes MUST be 0 at the All APIs scope.
// Non-zero values trigger response buffering that breaks MCP SSE streaming.
// See: https://learn.microsoft.com/en-us/azure/api-management/export-rest-mcp-server
resource apimDiagnostics 'Microsoft.ApiManagement/service/diagnostics@2024-06-01-preview' = {
  parent: apim
  name: 'applicationinsights'
  properties: {
    alwaysLog: 'allErrors'
    loggerId: apimLogger.id
    verbosity: 'verbose'
    logClientIp: true
    sampling: {
      samplingType: 'fixed'
      percentage: 100
    }
    frontend: {
      request: {
        headers: [
          'Authorization'
          'Content-Type'
          'Accept'
          'Host'
          'User-Agent'
          'X-Forwarded-For'
          'X-Forwarded-Host'
          'X-Request-ID'
          'X-MS-Client-Request-Id'
          'Mcp-Session-Id'
          'Cookie'
          'Sec-WebSocket-Protocol'
          'Connection'
          'Upgrade'
        ]
        body: {
          bytes: 8192
        }
      }
      response: {
        headers: [
          'WWW-Authenticate'
          'Content-Type'
          'Location'
          'X-MS-Request-Id'
          'Set-Cookie'
        ]
        body: {
          bytes: 0
        }
      }
    }
    backend: {
      request: {
        headers: [
          'Authorization'
          'Content-Type'
          'Host'
          'User-Agent'
        ]
        body: {
          bytes: 8192
        }
      }
      response: {
        headers: [
          'WWW-Authenticate'
          'Content-Type'
        ]
        body: {
          bytes: 0
        }
      }
    }
  }
}

// --- APIM Diagnostic Settings (GatewayLogs → Log Analytics) ---
resource apimDiagnosticSettings 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: 'apim-diagnostics'
  scope: apim
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'GatewayLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output apimGatewayUrl string = apim.properties.gatewayUrl
output apimName string = apim.name
output apimPrincipalId string = apim.identity.principalId
output apimResourceId string = apim.id
output ordersApiPath string = ordersApi.properties.path
