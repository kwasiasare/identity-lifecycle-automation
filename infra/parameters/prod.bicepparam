using '../main.bicep'

param namePrefix = 'idlc'
param environmentName = 'prod'
param location = 'eastus2' // pinned, not left to resourceGroup().location — Flex Consumption (FC1) is only available in specific regions (confirmed via `az functionapp list-flexconsumption-locations`); verify prod's target region is on that list before changing this.
param defaultDomain = 'yourtenant.onmicrosoft.com' // TODO: replace with the production tenant domain, once approved
param defaultUsageLocation = 'GB'
param leaverDeferredDeleteDays = 30
// dryRun stays true until the deferred-deletion sweep has real delete logic
// AND has been validated end-to-end on dev with Kwasi's explicit approval.
param dryRun = true
param welcomeMailSender = 'no-reply@yourtenant.onmicrosoft.com' // TODO: replace with the production tenant's dedicated no-reply mailbox UPN
