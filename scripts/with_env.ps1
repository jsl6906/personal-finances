param([Parameter(Mandatory)][string]$EnvFile, [Parameter(Mandatory, ValueFromRemainingArguments)][string[]]$Command)
# Run a backend command with variables from an extra env file layered over .env (e.g. .env.azure).
$ErrorActionPreference = "Stop"
$saved = @{}
Get-Content $EnvFile | Where-Object { $_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$' } | ForEach-Object {
    $saved[$Matches[1]] = [Environment]::GetEnvironmentVariable($Matches[1])
    Set-Item -Path "env:$($Matches[1])" -Value $Matches[2]
}
Push-Location (Join-Path (Split-Path $PSScriptRoot -Parent) "backend")
try { & uv run @Command; $code = $LASTEXITCODE }
finally {
    Pop-Location
    # Restore the caller's environment so later commands in this shell don't silently target the other database.
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
}
exit $code
