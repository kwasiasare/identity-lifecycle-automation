// Workspace-based Application Insights for Functions host telemetry
// (invocation traces, exceptions, dependency calls) — separate from the
// custom audit table, which carries business-level joiner/mover/leaver facts.
@description('Application Insights resource name.')
param appInsightsName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Resource ID of the Log Analytics workspace to link (workspace-based App Insights).')
param workspaceId string

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspaceId
    IngestionMode: 'LogAnalytics'
  }
}

output connectionString string = appInsights.properties.ConnectionString
// instrumentationKey is deliberately NOT output: it's the legacy/deprecated
// auth mechanism (Microsoft is retiring instrumentation-key-based ingestion),
// function-app.bicep only ever needed the connection string, and every extra
// credential-shaped output is one more thing that could be misused if a
// deployment's outputs are ever read by something other than this template.
