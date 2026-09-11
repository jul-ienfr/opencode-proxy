param(
  [Parameter(Mandatory=$true)][string]$Query,
  [int]$Limit = 25
)
# Force TLS 1.2 on Windows PowerShell 5.1
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}
$enc = [uri]::EscapeDataString($Query)
$url = "https://api.github.com/search/issues?q=$enc&per_page=$Limit&sort=updated"
try {
  $r = Invoke-WebRequest -Uri $url -UseBasicParsing -UserAgent 'dsh-research' -TimeoutSec 60
} catch {
  Write-Output "ERR: $($_.Exception.Message)"
  exit 1
}
$j = $r.Content | ConvertFrom-Json
Write-Output "total_count=$($j.total_count)"
foreach ($i in $j.items) {
  Write-Output ("#{0} [{1}] {2}`n    {3}" -f $i.number, $i.state, $i.title, $i.html_url)
}
