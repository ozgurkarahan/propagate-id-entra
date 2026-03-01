@description('Base name for resources')
param name string

@description('Azure region')
param location string

@description('Resource tags')
param tags object = {}

@description('ACR login server')
param registryLoginServer string

@description('ACR name')
param registryName string

@description('Log Analytics Workspace ID')
param logAnalyticsWorkspaceId string

@description('Application Insights connection string')
param appInsightsConnectionString string = ''

// Look up registry to get admin credentials
resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: registryName
}

// --- Container Apps Environment ---
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${name}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2023-09-01').customerId
        sharedKey: listKeys(logAnalyticsWorkspaceId, '2023-09-01').primarySharedKey
      }
    }
  }
}

// --- Orders API Container App ---
resource ordersApi 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'ca-orders-api'
  location: location
  tags: union(tags, {
    'azd-service-name': 'orders-api'
  })
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: registryLoginServer
          username: registry.listCredentials().username
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: registry.listCredentials().passwords[0].value
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'orders-api'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              value: appInsightsConnectionString
            }
            {
              name: 'OTEL_SERVICE_NAME'
              value: 'orders-api'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
    }
  }
}

output containerAppFqdn string = ordersApi.properties.configuration.ingress.fqdn
output containerAppName string = ordersApi.name
output containerAppsEnvironmentId string = environment.id
