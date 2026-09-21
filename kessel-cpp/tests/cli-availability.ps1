param(
  [Parameter(Mandatory = $true)][string]$Binary,
  [Parameter(Mandatory = $true)][string]$MockProvider,
  [Parameter(Mandatory = $true)][string]$TestRoot
)

$ErrorActionPreference = "Stop"
$binaryPath = (Resolve-Path -LiteralPath $Binary).Path
$mockPath = (Resolve-Path -LiteralPath $MockProvider).Path
$buildRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "../build"))
$testRootPath = [IO.Path]::GetFullPath($TestRoot)
if (-not $testRootPath.StartsWith($buildRoot, [StringComparison]::OrdinalIgnoreCase)) {
  throw "Test root must remain inside the C++ build directory"
}

function Assert-True([bool]$Condition, [string]$Message) {
  if (-not $Condition) { throw $Message }
}

function Set-Scenario([string]$Name) {
  $scenario = Join-Path $testRootPath $Name
  $config = Join-Path $scenario "config"
  $state = Join-Path $scenario "state"
  New-Item -ItemType Directory -Force $config, $state | Out-Null
  $env:KESSEL_CONFIG_DIR = $config
  $env:KESSEL_STATE_DIR = $state
  $env:KESSEL_ENFORCE_CLI_VERSIONS = "true"
  return Join-Path $config "config.json"
}

function Write-Config([string]$Path, [object]$Value) {
  $json = $Value | ConvertTo-Json
  [IO.File]::WriteAllText($Path, $json + "`n", [Text.UTF8Encoding]::new($false))
}

function Invoke-Kessel([string[]]$Arguments) {
  $previousPreference = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try {
    $output = (& $binaryPath @Arguments 2>&1 | Out-String)
    $exitCode = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previousPreference
  }
  return [pscustomobject]@{ ExitCode = $exitCode; Output = $output }
}

if (Test-Path -LiteralPath $testRootPath) {
  Remove-Item -LiteralPath $testRootPath -Recurse -Force
}
New-Item -ItemType Directory -Force $testRootPath | Out-Null

try {
  $configPath = Set-Scenario "run-before-setup"
  $result = Invoke-Kessel @("run", "--provider", "codex")
  Assert-True ($result.ExitCode -eq 1) "run before setup must fail"
  Assert-True ($result.Output.Contains("Kessel is not set up. Run: kessel-cpp setup")) `
    "run before setup must explain how to continue"
  Assert-True (-not (Test-Path -LiteralPath $configPath)) `
    "run before setup must not create configuration"

  $configPath = Set-Scenario "zero-providers"
  $zeroConfig = [ordered]@{
    api_key = $null
    host = "127.0.0.1"
    port = 18131
    codex_command = "missing-kessel-codex"
    claude_command = "missing-kessel-claude"
  }
  Write-Config $configPath $zeroConfig
  $before = Get-Content -LiteralPath $configPath -Raw
  $result = Invoke-Kessel @("setup")
  Assert-True ($result.ExitCode -eq 1) "setup without providers must fail"
  Assert-True ($result.Output.Contains("needs at least one installed and logged-in provider")) `
    "zero-provider setup must explain why it failed"
  Assert-True ($result.Output.Contains("run ``kessel-cpp setup`` again")) `
    "zero-provider setup must give a recovery command"
  Assert-True ((Get-Content -LiteralPath $configPath -Raw) -eq $before) `
    "zero-provider setup must not modify configuration"

  $zeroConfig.api_key = "kessel_test_key"
  Write-Config $configPath $zeroConfig
  $result = Invoke-Kessel @("serve")
  Assert-True ($result.ExitCode -eq 1) "server without providers must fail"
  Assert-True ($result.Output.Contains("needs at least one installed provider CLI")) `
    "server without providers must explain why it cannot start"

  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  $listener.Start()
  $zeroConfig.port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
  $listener.Stop()
  Write-Config $configPath $zeroConfig
  $env:KESSEL_ENFORCE_CLI_VERSIONS = "false"
  $stdout = Join-Path $env:KESSEL_STATE_DIR "zero-provider.out.log"
  $stderr = Join-Path $env:KESSEL_STATE_DIR "zero-provider.err.log"
  $server = Start-Process -FilePath $binaryPath -ArgumentList "serve" `
    -WindowStyle Hidden -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr -PassThru
  try {
    $healthStatus = 0
    for ($attempt = 0; $attempt -lt 100; $attempt++) {
      try {
        $response = Invoke-WebRequest -UseBasicParsing `
          "http://127.0.0.1:$($zeroConfig.port)/health"
        $healthStatus = [int]$response.StatusCode
      } catch {
        if ($null -ne $_.Exception.Response) {
          $healthStatus = [int]$_.Exception.Response.StatusCode
        }
      }
      if ($healthStatus -eq 503) { break }
      Start-Sleep -Milliseconds 100
    }
    Assert-True ($healthStatus -eq 503) `
      "health must be unavailable when zero providers are installed"
  } finally {
    [IO.File]::WriteAllText(
      (Join-Path $env:KESSEL_STATE_DIR "stop.request"),
      "stop`n",
      [Text.UTF8Encoding]::new($false)
    )
    $server.WaitForExit(15000) | Out-Null
    if (-not $server.HasExited) { Stop-Process -Id $server.Id -Force }
  }

  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  $listener.Start()
  $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
  $listener.Stop()
  $configPath = Set-Scenario "one-provider"
  $singleConfig = [ordered]@{
    api_key = $null
    host = "127.0.0.1"
    port = $port
    codex_command = $mockPath
    claude_command = "missing-kessel-claude"
  }
  Write-Config $configPath $singleConfig
  $result = Invoke-Kessel @("setup")
  Assert-True ($result.ExitCode -eq 0) "setup with one provider must succeed: $($result.Output)"
  Assert-True ($result.Output.Contains("Codex is ready")) `
    "single-provider setup must identify the working provider"
  Assert-True ($result.Output.Contains("the other provider is optional")) `
    "single-provider setup must explain that one provider is enough"
  Assert-True ($result.Output.Contains("OpenAI base URL (Codex)")) `
    "single-provider setup must print the working route"
  Assert-True (-not $result.Output.Contains("OpenAI base URL (Claude)")) `
    "single-provider setup must not advertise an unavailable route"
  Assert-True (-not $result.Output.Contains("Anthropic base URL")) `
    "Codex-only setup must not advertise Anthropic Messages"
  $saved = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  Assert-True (-not [string]::IsNullOrWhiteSpace($saved.api_key)) `
    "single-provider setup must generate an API key"
  Assert-True ($saved.codex_command -eq $mockPath) `
    "single-provider setup must save the working executable"
  Assert-True ($saved.claude_command -eq "missing-kessel-claude") `
    "single-provider setup must not replace the unavailable provider command"

  $configPath = Set-Scenario "one-claude-provider"
  $claudeMock = Join-Path (Split-Path $configPath -Parent) "mock-claude.exe"
  Copy-Item -LiteralPath $mockPath -Destination $claudeMock
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
  $listener.Start()
  $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
  $listener.Stop()
  $claudeConfig = [ordered]@{
    api_key = $null
    host = "127.0.0.1"
    port = $port
    codex_command = "missing-kessel-codex"
    claude_command = $claudeMock
  }
  Write-Config $configPath $claudeConfig
  $result = Invoke-Kessel @("setup")
  Assert-True ($result.ExitCode -eq 0) `
    "Claude-only setup must succeed: $($result.Output)"
  Assert-True ($result.Output.Contains("Claude Code is ready")) `
    "Claude-only setup must identify the working provider"
  Assert-True ($result.Output.Contains("OpenAI base URL (Claude)")) `
    "Claude-only setup must print its OpenAI-compatible route"
  Assert-True ($result.Output.Contains("Anthropic base URL")) `
    "Claude-only setup must print its Anthropic route"
  Assert-True (-not $result.Output.Contains("OpenAI base URL (Codex)")) `
    "Claude-only setup must not advertise the unavailable Codex route"
  Assert-True ($result.Output.Contains("kessel-cpp run --provider claude")) `
    "Claude-only setup must recommend the available provider"
  $saved = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
  Assert-True (-not [string]::IsNullOrWhiteSpace($saved.api_key)) `
    "Claude-only setup must generate an API key"
  Assert-True ($saved.claude_command -eq $claudeMock) `
    "Claude-only setup must save the working executable"
  Assert-True ($saved.codex_command -eq "missing-kessel-codex") `
    "Claude-only setup must not replace the unavailable provider command"
} finally {
  if (Test-Path -LiteralPath $testRootPath) {
    Remove-Item -LiteralPath $testRootPath -Recurse -Force
  }
}
