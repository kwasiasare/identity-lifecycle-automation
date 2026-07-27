// Storage account backing AzureWebJobsStorage: queue for event intake, blob
// container for CSV drop intake, table for the idempotency ledger fallback,
// and the Functions consumption plan's own content share.
@description('Storage account name (must be globally unique, lowercase, <=24 chars, no dashes).')
@minLength(3)
@maxLength(24)
param storageAccountName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

@description('Name of the queue that carries one message per joiner/mover/leaver event.')
param eventsQueueName string = 'identity-events'

@description('Name of the blob container HR/IT ops drops CSV exports into.')
param inboundContainerName string = 'identity-events-inbound'

@description('Name of the table used as the idempotency ledger fallback.')
param idempotencyTableName string = 'IdempotencyLedger'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource inboundContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: inboundContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource queueService 'Microsoft.Storage/storageAccounts/queueServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource eventsQueue 'Microsoft.Storage/storageAccounts/queueServices/queues@2023-01-01' = {
  parent: queueService
  name: eventsQueueName
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource idempotencyTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: idempotencyTableName
}

output id string = storageAccount.id
output name string = storageAccount.name
// Deliberately NOT outputting a connection string here (previously behind a
// linter-suppressed `outputs-should-not-contain-secrets`). Module outputs
// land in the deployment's activity log/history, so a secret output is a
// secret at rest wherever that history is retained — even though it's never
// written to source control. The account name is enough: function-app.bicep
// builds the connection string itself at the point of use via
// listKeys(resourceId(...)), scoped to exactly that one @secure() variable,
// never surfaced as a module output.
