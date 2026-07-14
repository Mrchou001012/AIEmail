$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from safe demo defaults. Fill secrets locally only when enabling integrations.'
}
docker compose build
docker compose run --rm migrate
docker compose up -d --wait
Write-Host 'MVP started and healthy at http://localhost:8000/docs'
