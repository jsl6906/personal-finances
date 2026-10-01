param([string]$Base = "http://127.0.0.1:8000", [string]$Password = "ledger-dev")
# Smoke-test a running Ledger instance: health, SPA routing, login, CRUD round trip.
$ErrorActionPreference = "Stop"
$s = New-Object Microsoft.PowerShell.Commands.WebRequestSession

function Call($method, $path, $body) {
    $args = @{ Uri = "$Base$path"; Method = $method; WebSession = $s; ContentType = "application/json" }
    if ($null -ne $body) { $args.Body = ($body | ConvertTo-Json -Depth 5) }
    Invoke-RestMethod @args
}

"health: " + ((Invoke-RestMethod "$Base/api/health") | ConvertTo-Json -Compress)
$spa = Invoke-WebRequest "$Base/transactions" -UseBasicParsing
"spa deep link: $($spa.StatusCode) $(if ($spa.Content -match '<title>Ledger</title>') { 'index.html served' } else { 'UNEXPECTED' })"
try { Invoke-RestMethod "$Base/api/transactions" | Out-Null; "auth guard: FAILED (no 401)" } catch { "auth guard: $($_.Exception.Response.StatusCode.value__)" }
Call POST "/api/auth/login" @{ password = $Password } | Out-Null
"me: " + ((Call GET "/api/auth/me") | ConvertTo-Json -Compress)

$stamp = Get-Date -Format "HHmmss"
$inst = Call POST "/api/institutions" @{ name = "Smoke Bank $stamp" }
$acct = Call POST "/api/accounts" @{ name = "Smoke Checking $stamp"; institution_id = $inst.id; account_type = "checking" }
$cats = Call GET "/api/categories"
$water = ($cats | Where-Object name -eq "Water").id
$t = Call POST "/api/transactions" @{ txn_date = (Get-Date -Format "yyyy-MM-dd"); description = "CITY OF SPRINGFIELD WATER $stamp"; amount = "-84.12"; account_id = $acct.id; category_id = $water }
"created txn $($t.id): $($t.description) $($t.amount) [$($t.category_group_name) > $($t.category_name)] via $($t.institution_name)"
$page = Call GET "/api/transactions?q=SPRINGFIELD&limit=5"
"list: total=$($page.total) out=$($page.total_out)"
$today = Get-Date
$start = (Get-Date -Year $today.Year -Month $today.Month -Day 1).ToString("yyyy-MM-dd")
$end = (Get-Date -Year $today.Year -Month $today.Month -Day 1).AddMonths(1).AddDays(-1).ToString("yyyy-MM-dd")
"summary: " + ((Call GET "/api/summary?start=$start&end=$end") | ConvertTo-Json -Compress)
Call DELETE "/api/transactions/$($t.id)" | Out-Null
"deleted txn $($t.id)"
