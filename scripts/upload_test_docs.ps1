# Upload test docs to H100 pipeline via tunneled API (localhost:18001).
# Prerequisite: ssh -L 18001:127.0.0.1:8001 -N docs-pipeline-host
$ErrorActionPreference = "Stop"
$Api = if ($env:PIPELINE_API) { $env:PIPELINE_API } else { "http://127.0.0.1:18001" }
$DocsDir = Join-Path $PSScriptRoot ".." "test docs" | Resolve-Path

Write-Host "API: $Api"
Write-Host "Docs: $DocsDir"

try {
  Invoke-RestMethod -Uri "$Api/health" -TimeoutSec 5 | ConvertTo-Json
} catch {
  Write-Error "API not reachable at $Api. Start SSH tunnel: ssh -L 18001:127.0.0.1:8001 -N docs-pipeline-host"
}

Get-ChildItem -Path $DocsDir -Filter "*.pdf" | ForEach-Object {
  Write-Host "`n========================================"
  Write-Host "UPLOAD: $($_.Name)"
  $boundary = [System.Guid]::NewGuid().ToString()
  $fileBytes = [System.IO.File]::ReadAllBytes($_.FullName)
  $fileEnc = [System.Text.Encoding]::GetEncoding("iso-8859-1").GetString($fileBytes)
  $LF = "`r`n"
  $bodyLines = @(
    "--$boundary",
    "Content-Disposition: form-data; name=`"file`"; filename=`"$($_.Name)`"",
    "Content-Type: application/pdf",
    "",
    $fileEnc,
    "--$boundary--",
    ""
  )
  $body = $bodyLines -join $LF
  $bodyBytes = [System.Text.Encoding]::GetEncoding("iso-8859-1").GetBytes($body)
  $resp = Invoke-RestMethod -Uri "$Api/upload?auto_approve=true" -Method Post `
    -ContentType "multipart/form-data; boundary=$boundary" -Body $bodyBytes
  $resp | ConvertTo-Json -Depth 5
}
