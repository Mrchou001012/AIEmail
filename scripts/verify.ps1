param(
    [int]$ApiPort = 18080,
    [switch]$KeepOnFailure
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $repoRoot 'docker-compose.yml'
$projectName = "ai-sales-agent-verify-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) $projectName
$runtimeDir = Join-Path $tempRoot 'runtime'
$assetsDir = Join-Path $tempRoot 'assets'
$verificationEnv = Join-Path $tempRoot 'verification.env'
$dbName = 'sales_agent_verify'
$testDbName = 'sales_agent_verify_test'
$dbUser = 'sales_agent_verify'
$dbPassword = 'sales_agent_verify_password'
$adminUser = 'verify-admin'
$adminPassword = 'verify-password'
$baseUri = "http://127.0.0.1:$ApiPort"
$succeeded = $false
$composeMayHaveResources = $false
$lastHealth = $null
$lastStatus = $null
$messageFile = $null

$runtimeEnvPath = $runtimeDir.Replace('\', '/')
$assetsEnvPath = $assetsDir.Replace('\', '/')
$serviceEnvPath = $verificationEnv.Replace('\', '/')
$safeEnvironment = [ordered]@{
    SERVICE_ENV_FILE = $serviceEnvPath
    API_PORT = [string]$ApiPort
    RUNTIME_HOST_DIR = $runtimeEnvPath
    ASSETS_HOST_DIR = $assetsEnvPath
    POSTGRES_DB = $dbName
    POSTGRES_USER = $dbUser
    POSTGRES_PASSWORD = $dbPassword
    DATABASE_URL = "postgresql+asyncpg://${dbUser}:${dbPassword}@db:5432/$dbName"
    DEMO_MODE = 'true'
    AI_PROVIDER = 'stub'
    ANTHROPIC_MODEL = 'claude-opus-4-8'
    ANTHROPIC_API_KEY = ''
    MAIL_TRANSPORT = 'file'
    MAIL_FROM = 'sales-agent@example.com'
    GMAIL_ADDRESS = ''
    GMAIL_APP_PASSWORD = ''
    IMAP_HOST = 'imap.gmail.com'
    IMAP_PORT = '993'
    IMAP_SYNC_ENABLED = 'false'
    IMAP_FOLDER = 'INBOX'
    IMAP_SENT_FOLDER = '[Gmail]/Sent Mail'
    IMAP_POLL_SECONDS = '20'
    JOB_LEASE_SECONDS = '900'
    OUTBOX_LEASE_SECONDS = '600'
    SMTP_HOST = 'smtp.gmail.com'
    SMTP_PORT = '465'
    SMTP_STARTTLS = 'false'
    DINGTALK_TRANSPORT = 'log'
    DINGTALK_WEBHOOK_URL = ''
    SAFE_MODE = 'true'
    AUTO_SEND_ENABLED = 'false'
    RECIPIENT_ALLOWLIST = 'internal@example.com'
    MAX_SENDS_PER_HOUR = '10'
    MAX_SENDS_PER_DAY = '30'
    ADMIN_USERNAME = $adminUser
    ADMIN_PASSWORD = $adminPassword
    PUBLIC_BASE_URL = $baseUri
    RUNTIME_DIR = '/app/runtime'
    CONTENT_DIR = '/app/config/content'
}
$savedEnvironment = @{}
foreach ($name in $safeEnvironment.Keys) {
    $savedEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
}

function Invoke-Compose([string[]]$ComposeArguments) {
    & docker @composeBaseArguments @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed: $($ComposeArguments -join ' ')"
    }
}

function Show-Diagnostics {
    Write-Host "Verification project: $projectName"
    Write-Host "Temporary files: $tempRoot"
    try {
        & docker @composeBaseArguments ps -a
        & docker @composeBaseArguments logs --no-color --tail 200 db migrate api worker
    }
    catch {
        Write-Warning "Unable to collect all Docker diagnostics: $($_.Exception.Message)"
    }
    if ($null -ne $lastHealth) {
        Write-Host "Last health response: $($lastHealth | ConvertTo-Json -Depth 6 -Compress)"
    }
    if ($null -ne $lastStatus) {
        Write-Host "Last admin status: $($lastStatus | ConvertTo-Json -Depth 6 -Compress)"
    }
}

Push-Location $repoRoot
try {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
    New-Item -ItemType Directory -Path $assetsDir -Force | Out-Null
    $envLines = foreach ($entry in $safeEnvironment.GetEnumerator()) {
        "$($entry.Key)=$($entry.Value)"
    }
    $utf8NoBom = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    [IO.File]::WriteAllLines($verificationEnv, [string[]]$envLines, $utf8NoBom)

    foreach ($entry in $safeEnvironment.GetEnumerator()) {
        $processValue = if ([string]::IsNullOrEmpty([string]$entry.Value)) { $null } else { [string]$entry.Value }
        [Environment]::SetEnvironmentVariable($entry.Key, $processValue, 'Process')
    }

    $composeBaseArguments = @(
        'compose',
        '-f', $composeFile,
        '--project-directory', $repoRoot,
        '--env-file', $verificationEnv,
        '-p', $projectName
    )

    $composeVersionText = & docker compose version --short
    if ($LASTEXITCODE -ne 0 -or -not $composeVersionText) {
        throw 'Docker Compose is unavailable.'
    }
    $composeVersion = [Version]($composeVersionText.Trim().TrimStart('v'))
    if ($composeVersion -lt [Version]'2.24.0') {
        throw "Docker Compose 2.24.0 or newer is required; found $composeVersion."
    }
    Write-Host "Docker Compose: $composeVersion"

    Invoke-Compose @('config', '--quiet')
    $dockerServerVersion = & docker info --format '{{.ServerVersion}}'
    if ($LASTEXITCODE -ne 0 -or -not $dockerServerVersion) {
        throw 'Docker Desktop Linux containers are not available. Start Docker Desktop and rerun verification.'
    }
    Write-Host "Docker server: $dockerServerVersion"

    $composeMayHaveResources = $true
    Invoke-Compose @('build')
    Invoke-Compose @('up', '-d', '--wait', '--wait-timeout', '120', 'db')
    Invoke-Compose @('exec', '-T', 'db', 'createdb', '-U', $dbUser, $testDbName)

    $testDatabaseUrl = "postgresql+asyncpg://${dbUser}:${dbPassword}@db:5432/$testDbName"
    Invoke-Compose @('run', '--rm', '--no-deps', '-e', "DATABASE_URL=$testDatabaseUrl", 'migrate', 'alembic', 'upgrade', 'head')
    Invoke-Compose @('run', '--rm', '--no-deps', '-e', "DATABASE_URL=$testDatabaseUrl", 'migrate', 'alembic', 'downgrade', '0001')
    Invoke-Compose @('run', '--rm', '--no-deps', '-e', "DATABASE_URL=$testDatabaseUrl", 'migrate', 'alembic', 'upgrade', 'head')

    Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'ruff', 'check', 'app', 'tests', 'alembic')
    Invoke-Compose @('run', '--rm', '--no-deps', 'api', 'python', '-m', 'compileall', '-q', 'app', 'tests', 'alembic')
    Invoke-Compose @('run', '--rm', '--no-deps', '-e', 'RUN_DB_INTEGRATION_TESTS=0', 'api', 'pytest', '-m', 'not integration')
    Invoke-Compose @(
        'run', '--rm', '--no-deps',
        '-e', "DATABASE_URL=$testDatabaseUrl",
        '-e', 'RUN_DB_INTEGRATION_TESTS=1',
        'api', 'pytest', '-m', 'integration'
    )

    Invoke-Compose @('run', '--rm', '--no-deps', 'migrate')
    Invoke-Compose @('up', '-d', '--wait', '--wait-timeout', '120', 'api', 'worker')

    $lastHealth = Invoke-RestMethod -Method Get -Uri "$baseUri/health" -TimeoutSec 10
    if ($lastHealth.status -ne 'ok' -or -not $lastHealth.database) {
        throw 'The API health response did not confirm database connectivity.'
    }

    $pair = "${adminUser}:$adminPassword"
    $token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes($pair))
    $headers = @{ Authorization = "Basic $token" }
    $lastStatus = Invoke-RestMethod -Method Get -Uri "$baseUri/admin/status" -Headers $headers -TimeoutSec 10
    if (
        $lastStatus.ai_provider -ne 'stub' -or
        $lastStatus.mail_transport -ne 'file' -or
        $lastStatus.dingtalk_transport -ne 'log' -or
        -not $lastStatus.safe_mode -or
        $lastStatus.auto_send_enabled -or
        $lastStatus.imap_sync_enabled
    ) {
        throw 'The isolated stack did not start with the required safe verification settings.'
    }
    if (
        $lastStatus.credentials_present.anthropic -or
        $lastStatus.credentials_present.gmail -or
        $lastStatus.credentials_present.dingtalk
    ) {
        throw 'External integration credentials leaked into the isolated verification stack.'
    }

    $outreachBody = @{ recipient = 'internal@example.com'; quantity = 100 } | ConvertTo-Json
    $outreach = Invoke-RestMethod `
        -Method Post `
        -Uri "$baseUri/admin/demo/outreach" `
        -Headers $headers `
        -ContentType 'application/json' `
        -Body $outreachBody `
        -TimeoutSec 10
    if (-not $outreach.queued -or $null -eq $outreach.job_id) {
        throw 'Fresh isolated demo outreach was not queued.'
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        $lastStatus = Invoke-RestMethod -Method Get -Uri "$baseUri/admin/status" -Headers $headers -TimeoutSec 10
        if ([int]$lastStatus.jobs.FAILED -gt 0) {
            throw 'The worker reported a failed job.'
        }
        if ([int]$lastStatus.outbox.FAILED -gt 0 -or [int]$lastStatus.outbox.CANCELLED -gt 0) {
            throw 'The file outbox reached a failed or cancelled state.'
        }
        if ([int]$lastStatus.outbox.UNKNOWN -gt 0) {
            throw 'The file outbox reached the SMTP-only UNKNOWN state.'
        }
        $workerState = & docker @composeBaseArguments ps --format json worker
        if ($LASTEXITCODE -ne 0 -or $workerState -notmatch 'running') {
            throw 'The worker container is not running.'
        }
        $messageFile = Get-ChildItem -Path (Join-Path $runtimeDir 'demo_outbox') -Filter '*.eml' -File -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ([int]$lastStatus.jobs.DONE -ge 1 -and [int]$lastStatus.outbox.SENT -ge 1 -and $null -ne $messageFile) {
            break
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($null -eq $messageFile) {
        throw 'Timed out waiting for the worker to write the demo email.'
    }
    if ([int]$lastStatus.jobs.DONE -lt 1 -or [int]$lastStatus.outbox.SENT -lt 1) {
        throw 'Timed out waiting for the demo job and outbox delivery to finish.'
    }

    $rawMessage = [IO.File]::ReadAllText($messageFile.FullName)
    if ($rawMessage -notmatch '(?im)^To:\s*internal@example\.com\s*$') {
        throw 'The generated email recipient was not internal@example.com.'
    }
    if ($rawMessage -notmatch '(?im)^Message-ID:\s*<[^>]+>\s*$') {
        throw 'The generated email did not contain a Message-ID.'
    }
    if ($rawMessage -notmatch 'Unit price: USD 100\.0000') {
        throw 'The generated email did not contain the expected deterministic price.'
    }

    $succeeded = $true
    Write-Host "Runnable MVP verification passed. Generated message: $($messageFile.FullName)"
}
catch {
    Write-Warning "Verification failed: $($_.Exception.Message)"
    if ($null -ne $composeBaseArguments) {
        Show-Diagnostics
    }
    throw
}
finally {
    try {
        $shouldCleanup = $succeeded -or -not $KeepOnFailure
        if ($shouldCleanup) {
            $dockerCleanupSucceeded = -not $composeMayHaveResources
            if ($composeMayHaveResources -and $null -ne $composeBaseArguments) {
                & docker @composeBaseArguments down -v --remove-orphans --rmi local
                if ($LASTEXITCODE -eq 0) {
                    $dockerCleanupSucceeded = $true
                }
                else {
                    Write-Warning "Docker cleanup failed for verification project $projectName. Resources were retained."
                }
            }
            if ($dockerCleanupSucceeded -and (Test-Path $tempRoot)) {
                try {
                    Remove-Item -Path $tempRoot -Recurse -Force -Confirm:$false
                }
                catch {
                    Write-Warning "Temporary directory cleanup failed: $($_.Exception.Message)"
                }
            }
        }
        else {
            Write-Host "Verification resources retained. Inspect with: docker compose -f `"$composeFile`" --project-directory `"$repoRoot`" --env-file `"$verificationEnv`" -p $projectName ps -a"
            Write-Host "Remove them with the same prefix plus: down -v --remove-orphans --rmi local"
            Write-Host "Temporary files retained at: $tempRoot"
        }
    }
    finally {
        foreach ($name in $safeEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], 'Process')
        }
        Pop-Location
    }
}
