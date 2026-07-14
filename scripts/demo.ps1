param(
    [string]$BaseUri = 'http://localhost:8000',
    [string]$AdminUsername = 'admin',
    [string]$AdminPassword = 'change-me-locally',
    [int]$TimeoutSeconds = 60
)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)

$pair = "${AdminUsername}:$AdminPassword"
$token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
$headers = @{ Authorization = "Basic $token" }

Invoke-RestMethod -Method Post -Uri "$BaseUri/admin/demo/seed" -Headers $headers | Out-Null
$before = Invoke-RestMethod -Method Get -Uri "$BaseUri/admin/status" -Headers $headers
$outreach = Invoke-RestMethod `
    -Method Post `
    -Uri "$BaseUri/admin/demo/outreach" `
    -Headers $headers `
    -ContentType 'application/json' `
    -Body '{"recipient":"internal@example.com","quantity":100}'

if ($outreach.queued) {
    Write-Host "Demo outreach job $($outreach.job_id) queued."
}
else {
    Write-Host 'Demo outreach was already queued previously; waiting for the idempotent result.'
}

$doneTarget = [int]$before.jobs.DONE
$sentTarget = [int]$before.outbox.SENT
if ($outreach.queued) {
    $doneTarget += 1
    $sentTarget += 1
}
else {
    $doneTarget = [Math]::Max(1, $doneTarget)
    $sentTarget = [Math]::Max(1, $sentTarget)
}
$failedBaseline = [int]$before.jobs.FAILED
$deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
$messageFile = $null
$status = $before

do {
    $status = Invoke-RestMethod -Method Get -Uri "$BaseUri/admin/status" -Headers $headers
    if ([int]$status.jobs.FAILED -gt $failedBaseline) {
        throw 'The demo worker reported a new failed job. Inspect docker compose logs worker.'
    }
    $messageFile = Get-ChildItem -Path 'runtime/demo_outbox' -Filter '*.eml' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ([int]$status.jobs.DONE -ge $doneTarget -and [int]$status.outbox.SENT -ge $sentTarget -and $null -ne $messageFile) {
        break
    }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $deadline)

if ($null -eq $messageFile -or [int]$status.jobs.DONE -lt $doneTarget -or [int]$status.outbox.SENT -lt $sentTarget) {
    throw "Timed out waiting for demo delivery. Check $BaseUri/admin/status and run docker compose logs --tail 200 api worker."
}

Write-Host "Demo email ready: $($messageFile.FullName)"
