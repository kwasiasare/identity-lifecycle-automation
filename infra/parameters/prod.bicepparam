using '../main.bicep'

param namePrefix = 'idlc'
param environmentName = 'prod'
param defaultDomain = 'yourtenant.onmicrosoft.com' // TODO: replace with the production tenant domain, once approved
param defaultUsageLocation = 'GB'
param leaverDeferredDeleteDays = 30
// dryRun stays true until the deferred-deletion sweep has real delete logic
// AND has been validated end-to-end on dev with Kwasi's explicit approval.
param dryRun = true
