// Log Analytics workspace + custom table + Data Collection Endpoint/Rule for
// the audit trail. The Function App's managed identity is granted the
// "Monitoring Metrics Publisher" role on the DCR so it can call the Logs
// Ingestion API without any shared key.
@description('Log Analytics workspace name.')
param workspaceName string

@description('Data Collection Endpoint name.')
param dceName string

@description('Data Collection Rule name.')
param dcrName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Retention in days for the workspace (dev tenants: keep short/cheap).')
param retentionInDays int = 30

@description('Principal ID of the managed identity that will publish audit logs.')
param publisherPrincipalId string

// Custom table name must end in _CL. The DCR stream name is "Custom-<tableName>".
var customTableName = 'IdentityLifecycleAudit_CL'
var streamName = 'Custom-${customTableName}'

var auditColumns = [
  { name: 'TimeGenerated', type: 'datetime' }
  { name: 'correlation_id', type: 'string' }
  { name: 'event_type', type: 'string' }
  { name: 'action', type: 'string' }
  { name: 'target', type: 'string' }
  { name: 'result', type: 'string' }
  { name: 'detail', type: 'string' }
  { name: 'actor', type: 'string' }
  { name: 'time_generated', type: 'string' }
  { name: 'extra', type: 'string' }
]

resource workspace 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
  }
}

resource auditTable 'Microsoft.OperationalInsights/workspaces/tables@2022-10-01' = {
  parent: workspace
  name: customTableName
  properties: {
    schema: {
      name: customTableName
      columns: auditColumns
    }
    retentionInDays: retentionInDays
  }
}

resource dce 'Microsoft.Insights/dataCollectionEndpoints@2022-06-01' = {
  name: dceName
  location: location
  tags: tags
  properties: {
    networkAcls: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource dcr 'Microsoft.Insights/dataCollectionRules@2022-06-01' = {
  name: dcrName
  location: location
  tags: tags
  properties: {
    dataCollectionEndpointId: dce.id
    streamDeclarations: {
      '${streamName}': {
        columns: auditColumns
      }
    }
    destinations: {
      logAnalytics: [
        {
          workspaceResourceId: workspace.id
          name: 'auditWorkspace'
        }
      ]
    }
    dataFlows: [
      {
        streams: [streamName]
        destinations: ['auditWorkspace']
        outputStream: streamName
      }
    ]
  }
  dependsOn: [
    auditTable
  ]
}

// Monitoring Metrics Publisher — the narrowest built-in role that can call
// the Logs Ingestion API for a specific DCR.
var monitoringMetricsPublisherRoleId = '3913510d-42f4-4e42-8a64-420c390055eb'

resource dcrRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dcr.id, publisherPrincipalId, monitoringMetricsPublisherRoleId)
  scope: dcr
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', monitoringMetricsPublisherRoleId)
    principalId: publisherPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output workspaceId string = workspace.id
output dceEndpoint string = dce.properties.logsIngestion.endpoint
output dcrImmutableId string = dcr.properties.immutableId
output streamName string = streamName
