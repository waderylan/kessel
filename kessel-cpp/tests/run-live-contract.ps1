$ErrorActionPreference = "Stop"
$root = (Resolve-Path "$PSScriptRoot/..").Path
$config = (Resolve-Path "$root/test-state/config").Path
$state = (Resolve-Path "$root/test-state/state").Path
$binary = "$root/build/bin/kessel-cpp.exe"
$stopFile = Join-Path $state "stop.request"
$stdout = Join-Path $state "server.out.log"
$stderr = Join-Path $state "server.err.log"

Remove-Item -LiteralPath $stopFile -ErrorAction SilentlyContinue
$env:KESSEL_CONFIG_DIR = $config
$env:KESSEL_STATE_DIR = $state
$env:KESSEL_ENFORCE_CLI_VERSIONS = "false"
$process = Start-Process $binary -ArgumentList "serve" -WorkingDirectory $root `
  -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
  -PassThru

try {
  $ready = $false
  for ($attempt = 0; $attempt -lt 100; $attempt++) {
    try {
      if ((Invoke-RestMethod "http://127.0.0.1:8000/health").status -eq "ok") {
        $ready = $true
        break
      }
    } catch {}
    Start-Sleep -Milliseconds 100
  }
  if (-not $ready) { throw "server did not start: $(Get-Content -Raw $stderr)" }
  & "$PSScriptRoot/contract.ps1"
} finally {
  [IO.File]::WriteAllText($stopFile, "stop", [Text.UTF8Encoding]::new($false))
  $process.WaitForExit(15000) | Out-Null
  if (-not $process.HasExited) { Stop-Process -Id $process.Id -Force }
}
