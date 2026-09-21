param([int]$Runs = 40)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot/..").Path
$repo = (Resolve-Path "$root/..").Path
$binary = "$root/build/bin/kessel-cpp.exe"
$mock = "$root/build/bin/kessel-mock-provider.exe"
$python = "$repo/.venv/Scripts/python.exe"
$out = "$root/benchmark-results"
New-Item -ItemType Directory -Force "$out/python-config", "$out/python-state", "$out/cpp-config", "$out/cpp-state" | Out-Null

function Write-Config([string]$Path, [int]$Port) {
  $content = @{ api_key="kessel_benchmark_key"; host="127.0.0.1"; port=$Port; codex_command=$mock; claude_command=$mock } | ConvertTo-Json
  [IO.File]::WriteAllText("$Path/config.json", $content, [Text.UTF8Encoding]::new($false))
}
Write-Config "$out/python-config" 18101
Write-Config "$out/cpp-config" 18102

function Wait-Health([int]$Port) {
  for ($i=0; $i -lt 100; $i++) { try { if ((Invoke-RestMethod "http://127.0.0.1:$Port/health").status -eq "ok") { return } } catch {}; Start-Sleep -Milliseconds 100 }
  throw "server on port $Port did not start"
}
function Measure-Server([int]$Port) {
  $headers = @{ Authorization="Bearer kessel_benchmark_key" }
  $body = '{"model":"default","messages":[{"role":"user","content":"benchmark"}]}'
  1..5 | ForEach-Object { Invoke-RestMethod -Method Post "http://127.0.0.1:$Port/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $body | Out-Null }
  $samples = foreach ($i in 1..$Runs) { $watch=[Diagnostics.Stopwatch]::StartNew(); $reply=Invoke-RestMethod -Method Post "http://127.0.0.1:$Port/v1/codex/chat/completions" -Headers $headers -ContentType application/json -Body $body; $watch.Stop(); if ($reply.choices[0].message.content -ne "MOCK_OK") { throw "bad benchmark response" }; $watch.Elapsed.TotalMilliseconds }
  $ordered = @($samples | Sort-Object); [pscustomobject]@{ runs=$Runs; mean_ms=[math]::Round(($samples | Measure-Object -Average).Average,3); p50_ms=[math]::Round($ordered[[math]::Floor($Runs*.5)],3); p95_ms=[math]::Round($ordered[[math]::Min($Runs-1,[math]::Floor($Runs*.95))],3) }
}

$env:KESSEL_ENFORCE_CLI_VERSIONS = "false"
$env:PYTHONDONTWRITEBYTECODE = "1"
$env:KESSEL_CONFIG_DIR = "$out/python-config"
$env:KESSEL_STATE_DIR = "$out/python-state"
$pythonProcess = Start-Process $python -ArgumentList "-m","app.cli","serve" -WorkingDirectory $repo -WindowStyle Hidden -PassThru
try { Wait-Health 18101; $pythonResult = Measure-Server 18101 } finally { Set-Content "$out/python-state/stop.request" "stop"; $pythonProcess.WaitForExit(10000) | Out-Null; if (-not $pythonProcess.HasExited) { $pythonProcess.Kill($true) } }
$env:KESSEL_CONFIG_DIR = "$out/cpp-config"
$env:KESSEL_STATE_DIR = "$out/cpp-state"
$cppProcess = Start-Process $binary -ArgumentList "serve" -WorkingDirectory $root -WindowStyle Hidden -PassThru
try { Wait-Health 18102; $cppResult = Measure-Server 18102 } finally { Set-Content "$out/cpp-state/stop.request" "stop"; $cppProcess.WaitForExit(10000) | Out-Null; if (-not $cppProcess.HasExited) { $cppProcess.Kill($true) } }

$gain = [math]::Round((1 - $cppResult.mean_ms / $pythonResult.mean_ms) * 100, 1)
$result = [pscustomobject]@{ measured_at=(Get-Date).ToString("o"); workload="sequential OpenAI-compatible requests through a fresh mock provider process"; python=$pythonResult; cpp=$cppResult; cpp_mean_latency_reduction_percent=$gain }
[IO.File]::WriteAllText("$out/results.json", ($result | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllLines("$out/results.md", @("# Python vs C++ gateway benchmark","","- Workload: $($result.workload)","- Runs: $Runs","- Python mean: $($pythonResult.mean_ms) ms","- C++ mean: $($cppResult.mean_ms) ms","- C++ mean latency reduction: $gain%","","Provider network latency is intentionally excluded by the deterministic mock provider."), [Text.UTF8Encoding]::new($false))
$result | ConvertTo-Json -Depth 5
