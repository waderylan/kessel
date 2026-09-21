param(
  [string]$BaseUrl = "http://127.0.0.1:8000",
  [string]$ConfigPath = "$PSScriptRoot/../test-state/config/config.json"
)

$ErrorActionPreference = "Stop"
$config = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($config.api_key)" }

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw "Contract failure: $Message" }
}

$health = Invoke-RestMethod "$BaseUrl/health"
Assert-True ($health.status -eq "ok") "health status"
Assert-True ($null -ne $health.providers.codex.available) "Codex health shape"
Assert-True ($null -ne $health.providers.claude.available) "Claude health shape"

try { Invoke-WebRequest -UseBasicParsing "$BaseUrl/v1/codex/models" | Out-Null; throw "unauthenticated request succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 401) "authentication status" }

try { Invoke-WebRequest -UseBasicParsing "$BaseUrl/v1/missing/models" -Headers $headers | Out-Null; throw "unknown provider succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 404) "unknown provider status" }

$oversizedBody = '{"model":"default","messages":[{"role":"user","content":"' + ('x' * 1048576) + '"}]}'
try { Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $oversizedBody | Out-Null; throw "oversized request succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 413) "request size status" }

$invalidBody = '{"model":"bad&model","messages":[{"role":"user","content":"x"}]}'
try { Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $invalidBody | Out-Null; throw "invalid model succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 400) "validation status" }

$parallelBody = '{"model":"default","parallel_tool_calls":true,"messages":[{"role":"user","content":"x"}]}'
try { Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $parallelBody | Out-Null; throw "parallel tools succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 400) "parallel tool rejection" }

$namedToolBody = '{"model":"default","messages":[{"role":"user","content":"weather"}],"tools":[{"name":"weather","input_schema":{"type":"object"}}],"tool_choice":{"type":"tool","name":"missing"}}'
try { Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/messages" -Headers $headers -ContentType application/json -Body $namedToolBody | Out-Null; throw "mismatched named tool succeeded" }
catch { Assert-True ($_.Exception.Response.StatusCode.value__ -eq 400) "named tool validation" }

$request = @{ model="default"; max_tokens=8; messages=@(@{role="user";content="Reply with exactly CONTRACT_OK."}) } | ConvertTo-Json -Depth 6 -Compress
$openai = Invoke-RestMethod -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $request -TimeoutSec 120
Assert-True ($openai.object -eq "chat.completion") "OpenAI object"
Assert-True ($openai.choices[0].message.role -eq "assistant") "OpenAI assistant role"

$anthropic = Invoke-RestMethod -Method Post "$BaseUrl/v1/messages" -Headers $headers -ContentType application/json -Body $request -TimeoutSec 120
Assert-True ($anthropic.type -eq "message") "Anthropic object"
Assert-True ($anthropic.role -eq "assistant") "Anthropic assistant role"

$streamRequest = @{ model="default"; stream=$true; stream_options=@{include_usage=$true}; messages=@(@{role="user";content="Reply with exactly STREAM_OK."}) } | ConvertTo-Json -Depth 6 -Compress
$stream = Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $streamRequest -TimeoutSec 120
Assert-True ($stream.Headers["Content-Type"] -like "text/event-stream*") "stream content type"
Assert-True ($stream.Content.EndsWith("data: [DONE]`n`n")) "stream terminator"
Assert-True ($stream.Content.Contains('"choices":[]')) "stream usage chunk"

$stopRequest = @{ model="default"; backend="warm"; stream=$true; stop=@("STOP"); messages=@(@{role="user";content="Reply exactly: before STOP after"}) } | ConvertTo-Json -Depth 6 -Compress
$stop = Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $stopRequest -TimeoutSec 120
Assert-True ($stop.Content.Contains('"finish_reason":"stop"')) "stop finish reason"
Assert-True (-not $stop.Content.Contains('STOP')) "stop sequence suppression"

$freshStopRequest = @{ model="default"; stream=$true; stop=@("STOP"); messages=@(@{role="user";content="Reply exactly: before STOP after"}) } | ConvertTo-Json -Depth 6 -Compress
$freshStop = Invoke-WebRequest -UseBasicParsing -Method Post "$BaseUrl/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $freshStopRequest -TimeoutSec 120
Assert-True ($freshStop.Content.Contains('"finish_reason":"stop"')) "fresh stop finish reason"
Assert-True (-not $freshStop.Content.Contains('"code":"stream_error"')) "fresh stream early-stop handling"

$parallelScript = {
  param($Url, $Headers, $Marker)
  $body = @{ model="default"; backend="warm"; messages=@(@{role="user";content="Reply with exactly $Marker"}) } | ConvertTo-Json -Depth 6 -Compress
  Invoke-RestMethod -Method Post "$Url/v1/codex/chat/completions" -Headers $Headers -ContentType application/json -Body $body -TimeoutSec 120
}
$warmJobs = @(
  Start-Job -ScriptBlock $parallelScript -ArgumentList $BaseUrl,$headers,"WARM_PARALLEL_A_731"
  Start-Job -ScriptBlock $parallelScript -ArgumentList $BaseUrl,$headers,"WARM_PARALLEL_B_947"
)
try {
  $warmResults = @($warmJobs | Receive-Job -Wait)
  $warmText = @($warmResults | ForEach-Object { $_.choices[0].message.content.Trim() })
  Assert-True ($warmText -contains "WARM_PARALLEL_A_731") "parallel warm response A"
  Assert-True ($warmText -contains "WARM_PARALLEL_B_947") "parallel warm response B"
} finally {
  $warmJobs | Remove-Job -Force
}

$page = Invoke-WebRequest -UseBasicParsing "$BaseUrl/"
Assert-True ($page.Content.Contains("One request. Your local subscription.")) "web client"
$docs = Invoke-WebRequest -UseBasicParsing "$BaseUrl/openapi.json"
Assert-True ($docs.StatusCode -eq 200) "OpenAPI document"

Write-Output "[ok] HTTP contract suite passed"
