using '../main.bicep'

param namePrefix = 'idlc'
param environmentName = 'dev'
param location = 'eastus2' // subscription has no Linux consumption quota in eastus; eastus2 is supported
param defaultDomain = 'spdcdev01.onmicrosoft.com'
param defaultUsageLocation = 'US'
param leaverDeferredDeleteDays = 30
param dryRun = true // safety rail: first e2e run validates flows without Graph writes, flip to false after
param welcomeMailSender = 'no-reply@spdcdev01.onmicrosoft.com' // TODO: create this shared mailbox in the dev tenant before flipping dryRun
