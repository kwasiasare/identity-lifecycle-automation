// User-assigned managed identity used by the Function App for all secretless
// auth: Microsoft Graph (via DefaultAzureCredential), Log Analytics Logs
// Ingestion API, and (optionally) identity-based storage bindings.
@description('Name of the user-assigned managed identity.')
param identityName string

@description('Azure region.')
param location string

@description('Resource tags.')
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
  tags: tags
}

output id string = identity.id
output principalId string = identity.properties.principalId
output clientId string = identity.properties.clientId
