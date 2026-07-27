using '../main.bicep'

param namePrefix = 'idlc'
param environmentName = 'dev'
param defaultDomain = 'yourtenant.onmicrosoft.com' // TODO: replace with the EP-7 dev tenant domain
param defaultUsageLocation = 'GB'
param leaverDeferredDeleteDays = 30
param dryRun = true
param welcomeMailSender = 'no-reply@yourtenant.onmicrosoft.com' // TODO: replace with the dev tenant's dedicated no-reply mailbox UPN
