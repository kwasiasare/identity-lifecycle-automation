// Flex Consumption (FC1) Linux Python Function App running the joiner/mover/
// leaver processor.
//
// Why Flex Consumption instead of the classic Y1 Consumption plan: the
// subscription this deploys into has zero quota for classic Linux Consumption
// serverfarms in every region (SubscriptionIsOverQuotaForSku, Total VMs limit
// 0) — confirmed via a failed Y1 deployment. Flex Consumption is a distinct
// SKU/quota family and is available in eastus2 (confirmed via
// `az functionapp list-flexconsumption-locations`), so this module targets
// FC1 instead. Flex also does away with the WEBSITE_CONTENTSHARE /
// WEBSITE_CONTENTAZUREFILECONNECTIONSTRING content-share model entirely —
// deployment package storage is configured explicitly below.
//
// Graph and Log Analytics auth are entirely managed-identity based (no client
// secrets anywhere). AzureWebJobsStorage still uses a connection string (see
// infra/README.md for why — short version: the queue/blob triggers in
// function_app.py bind via the "AzureWebJobsStorage" connection, and moving
// those to identity-based auth is a separate, deliberate change, not a side
// effect of the Flex migration) but that value is never accepted or emitted
// as a module parameter/output: this module takes only the storage account
// *name* and resolves listKeys() itself, right at the point the app setting
// is built — the narrowest possible blast radius for that secret within the
// template.
//
// The Flex Consumption *deployment package* storage (where the zip the
// pipeline publishes actually lives) is a separate, identity-based path per
// Microsoft's requirement: functionAppConfig.deployment.storage.type
// 'blobContainer' authenticated via the same user-assigned managed identity,
// which needs Storage Blob Data Contributor on the storage account — granted
// below via a role assignment (the deploying SP has Owner on the resource
// group, so this bicep-level role assignment is allowed).
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

@description('Principal (object) ID of the user-assigned managed identity — used for the Storage Blob Data Contributor role assignment required for Flex Consumption deployment storage authentication.')
param managedIdentityPrincipalId string

@description('Name of the storage account (same resource group) backing AzureWebJobsStorage — the connection string is derived here via listKeys(), never passed in or output.')
param storageAccountName string

@description('Blob service endpoint of the storage account (e.g. https://<name>.blob.core.windows.net/), from the storage module output — used to build the Flex Consumption deployment package container URL.')
param storageBlobEndpoint string

@description('Name of the blob container the Flex Consumption plan pulls the deployment package (zip) from. Must match the container created in storage.bicep.')
param deploymentPackageContainerName string = 'deploymentpackage'

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

@description('Dedicated service/no-reply mailbox UPN used to send the joiner welcome email — never the just-created user itself (see identity_lifecycle/flows/joiner.py).')
param welcomeMailSender string = ''

@description('Maximum instance count for the Flex Consumption plan.')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 40

@description('Per-instance memory (MB) for the Flex Consumption plan.')
@allowed([512, 2048, 4096])
param instanceMemoryMB int = 2048

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: storageAccountName
}

// Existing reference into the container created by storage.bicep — used only
// to scope the role assignment below as narrowly as possible (container, not
// whole account). Resolves safely: main.bicep passes storage.outputs.name
// into this module, which makes the functionApp module implicitly depend on
// the storage module, so the container already exists by the time this is
// evaluated.
resource deploymentPackageContainerRef 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' existing = {
  name: '${storageAccountName}/default/${deploymentPackageContainerName}'
}

var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

// Built-in "Storage Blob Data Contributor" role — required for the Flex
// Consumption plan's UserAssignedIdentity-authenticated deployment storage.
// Scoped to the deployment-package container only (not the whole storage
// account), so this identity cannot read/write the HR CSV inbound container
// or the idempotency/leaver-schedule tables through this grant.
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource deploymentStorageRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(deploymentPackageContainerRef.id, managedIdentityPrincipalId, storageBlobDataContributorRoleId)
  scope: deploymentPackageContainerRef
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: managedIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: '${functionAppName}-plan'
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
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
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          // storageBlobEndpoint (storage.bicep's primaryEndpoints.blob
          // output) always ends in '/' — that trailing slash is what makes
          // this a valid "<endpoint>/<container>" join, not a typo.
          value: '${storageBlobEndpoint}${deploymentPackageContainerName}'
          authentication: {
            type: 'UserAssignedIdentity'
            userAssignedIdentityResourceId: managedIdentityId
          }
        }
      }
      scaleAndConcurrency: {
        maximumInstanceCount: maximumInstanceCount
        instanceMemoryMB: instanceMemoryMB
      }
      runtime: {
        name: 'python'
        version: '3.11'
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        // FUNCTIONS_WORKER_RUNTIME / WEBSITE_RUN_FROM_PACKAGE and the
        // WEBSITE_CONTENTAZUREFILECONNECTIONSTRING / WEBSITE_CONTENTSHARE
        // content-share pair are deliberately NOT set here — on Flex
        // Consumption the worker runtime + version come from
        // functionAppConfig.runtime above, there is no SCM content share,
        // and the deployment package location/auth come from
        // functionAppConfig.deployment.storage above.
        { name: 'FUNCTIONS_EXTENSION_VERSION', value: '~4' }
        // Python 3.11 (unlike 3.13+) does not run worker function indexing
        // during cold-start init by default — it needs this flag to opt in.
        // Without it we observed the host log "Reading functions metadata
        // (Custom)" -> "0 functions found (Custom)" in well under a
        // millisecond on every cold start: the host never even attempted to
        // launch the Python worker to index function_app.py, which is
        // exactly the symptom this app hit after moving to Flex Consumption
        // (see README "Known gap on Flex Consumption" / CI history). See
        // https://learn.microsoft.com/azure/azure-functions/python-build-options
        // ("Module import has a 2-minute time limit ... for older python
        // versions with PYTHON_ENABLE_INIT_INDEXING enabled").
        { name: 'PYTHON_ENABLE_INIT_INDEXING', value: '1' }
        { name: 'AzureWebJobsStorage', value: storageConnectionString }
        { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsightsConnectionString }
        { name: 'AZURE_CLIENT_ID', value: managedIdentityClientId }
        { name: 'GRAPH_BASE_URL', value: 'https://graph.microsoft.com/v1.0' }
        { name: 'GRAPH_BETA_URL', value: 'https://graph.microsoft.com/beta' }
        { name: 'DEFAULT_DOMAIN', value: defaultDomain }
        { name: 'DEFAULT_USAGE_LOCATION', value: defaultUsageLocation }
        { name: 'EVENTS_QUEUE_NAME', value: 'identity-events' }
        { name: 'INBOUND_CONTAINER_NAME', value: 'identity-events-inbound' }
        { name: 'IDEMPOTENCY_TABLE_NAME', value: 'IdempotencyLedger' }
        { name: 'LEAVER_SCHEDULE_TABLE_NAME', value: 'LeaverSchedule' }
        { name: 'LOGS_INGESTION_ENDPOINT', value: logsIngestionEndpoint }
        { name: 'LOGS_DCR_IMMUTABLE_ID', value: dcrImmutableId }
        { name: 'LOGS_STREAM_NAME', value: logsStreamName }
        { name: 'LEAVER_DEFERRED_DELETE_DAYS', value: string(leaverDeferredDeleteDays) }
        { name: 'WELCOME_MAIL_SENDER', value: welcomeMailSender }
        { name: 'DRY_RUN', value: string(dryRun) }
        { name: 'ENVIRONMENT_NAME', value: environmentName }
      ]
    }
  }
  dependsOn: [
    deploymentStorageRoleAssignment
  ]
}

output name string = functionApp.name
output defaultHostName string = functionApp.properties.defaultHostName
// Note: the managed identity's principalId (used for role assignments) comes
// from the managed-identity module output, not from this resource — a
// UserAssigned identity block does not expose `identity.principalId` the way
// SystemAssigned does.
