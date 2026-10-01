# Writes \\MediaServer\Docker\ledger\.env from the local .env + .env.azure without printing secrets.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$share = "\\MediaServer\Docker\ledger"

function Read-Env($path) {
    $h = [ordered]@{}
    foreach ($line in Get-Content $path) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $h[$Matches[1]] = $Matches[2] }
    }
    $h
}
$local = Read-Env "$root\.env"
$azure = Read-Env "$root\.env.azure"
if (Test-Path "$share\.env") { throw "$share\.env already exists; edit it by hand instead" }

$bytes = New-Object byte[] 48
[Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$session = [Convert]::ToBase64String($bytes) -replace '[+/=]', ''

$lines = @("# Ledger production settings (Azure Postgres via service principal)")
foreach ($k in $azure.Keys) { $lines += "$k=$($azure[$k])" }
$lines += "", "APP_PASSWORD_HASH=$($local['APP_PASSWORD_HASH'])", "SESSION_SECRET=$session", "COOKIE_SECURE=false"
$lines += "GEMINI_KEY=$($local['GEMINI_KEY'])", "", "GOOGLE_SERVICE_ACCOUNT_FILE=/secrets/google-sa.json", ""
foreach ($k in "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_FROM", "SMTP_PASSWORD") { $lines += "$k=$($local[$k])" }
$lines += "APP_BASE_URL=http://192.168.86.201:8470", "TZ=America/New_York"

New-Item -ItemType Directory -Force "$share\secrets" | Out-Null
[IO.File]::WriteAllText("$share\.env", ($lines -join "`n") + "`n")
Copy-Item "$root\secrets\google-sa.json" "$share\secrets\google-sa.json"
Write-Host "Wrote $share\.env ($($lines.Count) lines) and secrets\google-sa.json"
