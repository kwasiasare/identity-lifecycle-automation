// Identity Lifecycle Automation — top-level infrastructure.
//
// Deploys, per environment (dev/prod):
//   - a user-assigned managed identity (Graph + Log Analytics auth, no secrets)
//   - a storage account (queue intake, blob CSV drop, idempotency table)
//   - a Log Analytics workspace + custom audit table + DCE/DCR
//   - workspace-based Application Insights
//   - a Consumption-plan Linux Python Function App
//
// Dev/prod are two deployments of the same template with different
// parameter files (infra/parameters/dev.bicepparam, prod.bicepparam) and
// naming — per the program's Azure rule, "dev" here means a distinctly-named
// but architecturally identical resource group, not a shared/prod resource
// with a preview slot (that SWA-preview-environment rule applies to project 6,
// the portfolio site; this project's dev/prod separation is tenant-level, see
// README "Dev vs prod tenant" section).

targetScope = 'resourceGroup'

@description('Short project prefix used in every resource name.')
param namePrefix string = 'idlc'

@description('Environment name: dev or prod.')
@allowed(['dev', 'prod'])
param environmentName string = 'dev'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Default Entra tenant domain used for new user creation, e.g. contoso.onmicrosoft.com.')
param defaultDomain string

@description('Default usageLocation (ISO 3166-1 alpha-2) applied to new users.')
param defaultUsageLocation string = 'GB'

@description('Deferred-deletion window, in days, for leaver accounts.')
param leaverDeferredDeleteDays int = 30

@description('When true, destructive/production-affecting behaviour stays disabled regardless of other settings. Defaults true in both environments until the deferred-deletion sweep is implemented past its v1 stub.')
param dryRun bool = true

@description('Log Analytics retention in days.')
param logAnalyticsRetentionInDays int = environmentName == 'prod' ? 90 : 30

var tags = {
  project: 'identity-lifecycle-automation'
  environment: environmentName
  'managed-by': 'bicep'
}

// Storage account names must be globally unique, <=24 chars, lowercase, no
// dashes — derive a short deterministic suffix from the resource group.
var uniqueSuffix = uniqueString(resourceGroup().id)
var storageAccountName = toLower('${namePrefix}${environmentName}st${uniqueSuffix}')

var identityName = '${namePrefix}-${environmentName}-uami'
var workspaceName = '${namePrefix}-${environmentName}-law'
var dceName = '${namePrefix}-${environmentName}-dce'
var dcrName = '${namePrefix}-${environmentName}-dcr'
var appInsightsName = '${namePrefix}-${environmentName}-appi'
var functionAppName = '${namePrefix}-${environmentName}-func-${uniqueSuffix}'

module identity 'modules/managed-identity.bicep' = {
  name: 'identity'
  params: {
    identityName: identityName
    location: location
    tags: tags
  }
}

module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    storageAccountName: storageAccountName
    location: location
    tags: tags
  }
}

module logAnalytics 'modules/log-analytics.bicep' = {
  name: 'logAnalytics'
  params: {
    workspaceName: workspaceName
    dceName: dceName
    dcrName: dcrName
    location: location
    tags: tags
    retentionInDays: logAnalyticsRetentionInDays
    publisherPrincipalId: identity.outputs.principalId
  }
}

module appInsights 'modules/app-insights.bicep' = {
  name: 'appInsights'
  params: {
    appInsightsName: appInsightsName
    location: location
    tags: tags
    workspaceId: logAnalytics.outputs.workspaceId
  }
}

module functionApp 'modules/function-app.bicep' = {
  name: 'functionApp'
  params: {
    functionAppName: functionAppName
    location: location
    tags: tags
    environmentName: environmentName
    managedIdentityId: identity.outputs.id
    managedIdentityClientId: identity.outputs.clientId
    storageConnectionString: storage.outputs.connectionString
    appInsightsConnectionString: appInsights.outputs.connectionString
    logsIngestionEndpoint: logAnalytics.outputs.dceEndpoint
    dcrImmutableId: logAnalytics.outputs.dcrImmutableId
    logsStreamName: logAnalytics.outputs.streamName
    defaultDomain: defaultDomain
    defaultUsageLocation: defaultUsageLocation
    leaverDeferredDeleteDays: leaverDeferredDeleteDays
    dryRun: dryRun
  }
}

output functionAppName string = functionApp.outputs.name
output functionAppHostName string = functionApp.outputs.defaultHostName
output managedIdentityClientId string = identity.outputs.clientId
output managedIdentityPrincipalId string = identity.outputs.principalId
output storageAccountName string = storage.outputs.name
output logAnalyticsWorkspaceId string = logAnalytics.outputs.workspaceId
