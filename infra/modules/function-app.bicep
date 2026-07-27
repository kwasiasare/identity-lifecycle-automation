// Consumption-plan (Y1) Linux Python Function App running the joiner/mover/
// leaver processor. Graph and Log Analytics auth are entirely managed-identity
// based (no client secrets anywhere). AzureWebJobsStorage still uses a
// deploy-time-resolved connection string (see infra/README.md for why —
// short version: identity-based storage triggers have real limitations on
// the Consumption plan today) but that value is never written to source
// control; Bicep resolves it from listKeys() at deployment time only.
@description('Function App name.')
param functionAppName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Environment name: dev or prod.')
@allowed(['dev', 'prod'])
param environmentName string

@description('Resource ID of the user-assigned managed identity.')
param managedIdentityId string

@description('Client ID of the user-assigned managed identity (so DefaultAzureCredential picks the right identity).')
param managedIdentityClientId string

@description('Storage account connection string (deploy-time only, never committed).')
@secure()
param storageConnectionString string

@description('Application Insights connection string.')
param appInsightsConnectionString string

@description('Data Collection Endpoint logs ingestion URL.')
param logsIngestionEndpoint string

@description('Data Collection Rule immutable ID.')
param dcrImmutableId string

@description('Data Collection Rule stream name for the audit table.')
param logsStreamName string

@description('Default Entra tenant domain used for new user creation, e.g. contoso.onmicrosoft.com.')
param defaultDomain string

@description('Default usageLocation (ISO 3166-1 alpha-2) applied to new users.')
param defaultUsageLocation string = 'GB'

@description('Deferred-deletion window, in days, for leaver accounts.')
param leaverDeferredDeleteDays int = 30

@description('When true, destructive/production-affecting behaviour stays disabled regardless of other settings.')
param dryRun bool = true

resource hostingPlan 'Microsoft.Web/serverfarms@2023-01-01' = {
  name: '${functionAppName}-plan'
  location: location
  tags: tags
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  kind: 'functionapp,linux'
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-01-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.11'
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        { name: 'FUNCTIONS_WORKER_RUNTIME', value: 'python' }
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        { name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING', value: storageConnectionString }
        { name: 'WEBSITE_CONTENTSHARE', value: toLower(functionAppName) }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        { name: 'AZURE_CLIENT_ID', value: managedIdentityClientId }
        { name: 'GRAPH_BASE_URL', value: 'https://graph.microsoft.com/v1.0' }
        { name: 'GRAPH_BETA_URL', value: 'https://graph.microsoft.com/beta' }
        { name: 'DEFAULT_DOMAIN', value: defaultDomain }
        { name: 'DEFAULT_USAGE_LOCATION', value: defaultUsageLocation }
        { name: 'EVENTS_QUEUE_NAME', value: 'identity-events' }
        { name: 'INBOUND_CONTAINER_NAME', value: 'identity-events-inbound' }
        { name: 'IDEMPOTENCY_TABLE_NAME', value: 'IdempotencyLedger' }
        { name: 'LOGS_INGESTION_ENDPOINT', value: logsIngestionEndpoint }
        { name: 'LOGS_DCR_IMMUTABLE_ID', value: dcrImmutableId }
        { name: 'LOGS_STREAM_NAME', value: logsStreamName }
        { name: 'LEAVER_DEFERRED_DELETE_DAYS', value: string(leaverDeferredDeleteDays) }
        { name: 'DRY_RUN', value: string(dryRun) }
        { name: 'ENVIRONMENT_NAME', value: environmentName }
      ]
    }
  }
}

output name string = functionApp.name
output defaultHostName string = functionApp.properties.defaultHostName
// Note: the managed identity's principalId (used for role assignments) comes
// from the managed-identity module output, not from this resource — a
// UserAssigned identity block does not expose `identity.principalId` the way
// SystemAssigned does.
