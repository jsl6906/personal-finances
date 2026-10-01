param(
    [string]$AppName = "ledger-app",
    [string]$Subscription = "fee4a0bc-282c-4493-a3d4-f961950634cd"
)
# Creates the Entra service principal the container uses to log in to Azure Postgres,
# registers it as a Postgres role, and writes its credentials to .env.azure (gitignored).
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$out = Join-Path $root ".env.azure"

$existing = az ad sp list --display-name $AppName --query "[0].appId" -o tsv
if ($existing) {
    Write-Host "Service principal '$AppName' already exists (appId $existing); resetting its secret."
    $cred = az ad app credential reset --id $existing --display-name "ledger" --years 2 -o json | ConvertFrom-Json
} else {
    # No role assignment: the SP only needs a Postgres login, not Azure RBAC.
    $cred = az ad sp create-for-rbac --name $AppName --years 2 -o json | ConvertFrom-Json
}
if ($LASTEXITCODE -ne 0 -or -not $cred.appId) { throw "Service principal creation failed" }

@"
DB_HOST=jsl6906.postgres.database.azure.com
DB_PORT=5432
DB_NAME=personal_storage
DB_SCHEMA=personal_finances
DB_USER=$AppName
DB_AUTH_MODE=entra
DB_SSL=require
AZURE_TENANT_ID=$($cred.tenant)
AZURE_CLIENT_ID=$($cred.appId)
AZURE_CLIENT_SECRET=$($cred.password)
"@ | Out-File -FilePath $out -Encoding ascii
Write-Host "Wrote credentials to $out (merge into the server's .env)."

Push-Location (Join-Path $root "backend")
try {
    uv run python scripts/provision_azure_db.py --principal $AppName
    if ($LASTEXITCODE -ne 0) { throw "Database provisioning failed" }
} finally { Pop-Location }
