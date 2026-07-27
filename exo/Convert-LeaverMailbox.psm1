<#
.SYNOPSIS
    Converts a leaver's Exchange Online mailbox to a shared mailbox and blocks
    interactive sign-in to it, as the leaver flow's mailbox-conversion step.

.DESCRIPTION
    Deliberately isolated from the Python Azure Function. Exchange Online
    mailbox management (Set-Mailbox -Type Shared, mailbox permission grants
    for the departed user's manager, etc.) is exposed through the Exchange
    Online PowerShell module (ExchangeOnlineManagement), not Microsoft Graph
    application permissions in a way that's practical for a Consumption-plan
    Python Function App. Rather than shell out to PowerShell from Python (or
    duplicate auth plumbing for a second identity), v1 keeps this as an
    explicit, auditable, separately-run step:

      - Manual: an admin runs Convert-LeaverMailbox after checking the
        function's audit log for a "flag_mailbox_conversion" record.
      - Automated (recommended once validated manually a few times): wire this
        module into an Azure Automation runbook or a scheduled GitHub Actions
        job using an Az PowerShell OIDC-federated identity with the Exchange
        Administrator role, triggered by querying the audit table for
        unconverted leavers.

    Every mutation here is check-before-write, same discipline as the Python
    GraphClient: re-running against an already-converted mailbox is a no-op.

.NOTES
    Requires: ExchangeOnlineManagement module, an account/identity with the
    Exchange Administrator (or Recipient Management) role.
    Auth: Connect-ExchangeOnline supports certificate-based app-only auth and
    managed-identity auth from Azure Automation — no interactive password/
    secret should ever be embedded in automation that calls this module.
#>

function Convert-LeaverMailbox {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)]
        [string] $UserPrincipalName,

        [Parameter()]
        [string] $GrantAccessToManagerUpn,

        [Parameter()]
        [switch] $WhatIfOnly
    )

    if (-not (Get-Command -Name Get-Mailbox -ErrorAction SilentlyContinue)) {
        throw "ExchangeOnlineManagement module not loaded. Run Connect-ExchangeOnline first."
    }

    $mailbox = Get-Mailbox -Identity $UserPrincipalName -ErrorAction SilentlyContinue
    if (-not $mailbox) {
        Write-Warning "No mailbox found for $UserPrincipalName — nothing to convert."
        return [pscustomobject]@{
            UserPrincipalName = $UserPrincipalName
            Action            = "convert_to_shared"
            Result            = "skipped"
            Detail            = "mailbox not found"
        }
    }

    if ($mailbox.RecipientTypeDetails -eq "SharedMailbox") {
        Write-Verbose "$UserPrincipalName is already a shared mailbox — idempotent no-op."
        $convertResult = [pscustomobject]@{
            UserPrincipalName = $UserPrincipalName
            Action            = "convert_to_shared"
            Result            = "skipped"
            Detail            = "already a shared mailbox"
        }
    }
    elseif ($WhatIfOnly -or -not $PSCmdlet.ShouldProcess($UserPrincipalName, "Convert to shared mailbox")) {
        $convertResult = [pscustomobject]@{
            UserPrincipalName = $UserPrincipalName
            Action            = "convert_to_shared"
            Result            = "whatif"
            Detail            = "would convert mailbox type from $($mailbox.RecipientTypeDetails) to SharedMailbox"
        }
    }
    else {
        Set-Mailbox -Identity $UserPrincipalName -Type Shared
        $convertResult = [pscustomobject]@{
            UserPrincipalName = $UserPrincipalName
            Action            = "convert_to_shared"
            Result            = "success"
            Detail            = "converted from $($mailbox.RecipientTypeDetails) to SharedMailbox"
        }
    }

    $results = @($convertResult)

    if ($GrantAccessToManagerUpn) {
        $existingPermission = Get-MailboxPermission -Identity $UserPrincipalName -User $GrantAccessToManagerUpn -ErrorAction SilentlyContinue |
            Where-Object { $_.AccessRights -contains "FullAccess" }

        if ($existingPermission) {
            $results += [pscustomobject]@{
                UserPrincipalName = $UserPrincipalName
                Action            = "grant_manager_access"
                Result            = "skipped"
                Detail            = "$GrantAccessToManagerUpn already has FullAccess"
            }
        }
        elseif ($WhatIfOnly -or -not $PSCmdlet.ShouldProcess($UserPrincipalName, "Grant FullAccess to $GrantAccessToManagerUpn")) {
            $results += [pscustomobject]@{
                UserPrincipalName = $UserPrincipalName
                Action            = "grant_manager_access"
                Result            = "whatif"
                Detail            = "would grant FullAccess + AutoMapping to $GrantAccessToManagerUpn"
            }
        }
        else {
            Add-MailboxPermission -Identity $UserPrincipalName -User $GrantAccessToManagerUpn `
                -AccessRights FullAccess -InheritanceType All -AutoMapping $true | Out-Null
            $results += [pscustomobject]@{
                UserPrincipalName = $UserPrincipalName
                Action            = "grant_manager_access"
                Result            = "success"
                Detail            = "granted FullAccess + AutoMapping to $GrantAccessToManagerUpn"
            }
        }
    }

    return $results
}

Export-ModuleMember -Function Convert-LeaverMailbox
