param(
  [int]$Port = 3088,
  [string]$InputDevice = '',
  [double]$InputGain = 2.0,
  [double]$VadThreshold = 0.18,
  [switch]$AllowOutputInput
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# Load .env files
foreach ($envFile in @(
  (Join-Path $root ".env"),
  (Join-Path $env:USERPROFILE ".env")
)) {
  if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
      if ($_ -match '^([^#][^=]+)=(.*)$') {
        $k = $matches[1].Trim()
        $v = $matches[2].Trim()
        if (-not [System.Environment]::GetEnvironmentVariable($k, 'Process')) {
          [System.Environment]::SetEnvironmentVariable($k, $v, 'Process')
        }
      }
    }
  }
}

# Find Python
$py = 'python'
$pyPath = 'C:\Users\Patrick\AppData\Local\Programs\Python\Python312\python.exe'
if (Test-Path $pyPath) { $py = $pyPath }

Write-Output '[realtime] stopping existing processes...'
Get-CimInstance Win32_Process |
  Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'realtime[\\/]realtime\.py|realtime[\\/]viewer\.py|-m realtime' } |
  ForEach-Object {
    try {
      Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
      Write-Output ("[realtime] stopped pid={0}" -f $_.ProcessId)
    } catch {}
  }

if ([string]::IsNullOrWhiteSpace($InputDevice)) {
  Remove-Item Env:VOICE_INPUT_DEVICE -ErrorAction SilentlyContinue
} else {
  $env:VOICE_INPUT_DEVICE = $InputDevice
}

$env:VOICE_INPUT_GAIN = [string]$InputGain
$env:VOICE_VAD_THRESHOLD = [string]$VadThreshold
if ($AllowOutputInput) { $env:VOICE_ALLOW_OUTPUT_INPUT = '1' } else { Remove-Item Env:VOICE_ALLOW_OUTPUT_INPUT -ErrorAction SilentlyContinue }

$logDir = Join-Path $root 'runtime_logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$rtOut = Join-Path $logDir ("realtime_{0}.out.log" -f $ts)
$rtErr = Join-Path $logDir ("realtime_{0}.err.log" -f $ts)
$vwOut = Join-Path $logDir ("viewer_{0}.out.log" -f $ts)
$vwErr = Join-Path $logDir ("viewer_{0}.err.log" -f $ts)

Write-Output '[realtime] starting daemon...'
$rtProc = Start-Process -FilePath $py -ArgumentList '-u', '-m', 'realtime' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $rtOut -RedirectStandardError $rtErr -PassThru
Write-Output ("[realtime] daemon pid={0}" -f $rtProc.Id)
Start-Sleep -Milliseconds 900

Write-Output '[realtime] starting viewer...'
$vwProc = Start-Process -FilePath $py -ArgumentList '-u', "$root\realtime\viewer.py", '--port', "$Port", '--no-open' -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $vwOut -RedirectStandardError $vwErr -PassThru
Write-Output ("[realtime] viewer pid={0}" -f $vwProc.Id)
Start-Sleep -Milliseconds 800

$statusUrl = "http://127.0.0.1:$Port/status"
$ok = $false
for ($i = 0; $i -lt 12; $i++) {
  try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri $statusUrl -TimeoutSec 4
    if ($resp.StatusCode -eq 200) {
      $st = $resp.Content | ConvertFrom-Json
      Write-Output ("[realtime] OK connected={0} mic={1}" -f $st.connected, $st.mic_device)
      $ok = $true
      break
    }
  } catch {
    Start-Sleep -Milliseconds 700
  }
}
if (-not $ok) {
  Write-Output '[realtime] status check failed after retries'
  exit 1
}

Write-Output ("[realtime] viewer: http://127.0.0.1:{0}" -f $Port)
