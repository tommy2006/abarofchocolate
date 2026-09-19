# Trustworthy Process Monitor - one-command launcher for Windows (PowerShell 5.1+ / 7+).
#   .\run.ps1                 install (first time) + start the UI on http://127.0.0.1:8000
#   .\run.ps1 -Demo           also generate sample data and run the pipeline on it first
#   .\run.ps1 -PullModels     pull the configured Ollama model if Ollama is installed
#   .\run.ps1 -Port 8080      another port
#   .\run.ps1 -NoOpen         do not open the browser
#   .\run.ps1 -Doctor         only run the environment check
param(
    [switch]$Demo,
    [switch]$PullModels,
    [switch]$NoOpen,
    [switch]$Doctor,
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Write-Banner($text) { Write-Host ""; Write-Host ("=" * 64) -ForegroundColor Cyan; Write-Host " $text" -ForegroundColor Cyan; Write-Host ("=" * 64) -ForegroundColor Cyan }
function Find-Python {
    $candidates = @()
    foreach ($c in @("py -3.12", "py -3.11", "py -3.10", "py -3", "python", "python3")) { $candidates += $c }
    foreach ($cand in $candidates) {
        $parts = $cand.Split(" ")
        $exe = $parts[0]; $args = @(); if ($parts.Length -gt 1) { $args = $parts[1..($parts.Length - 1)] }
        try {
            $v = & $exe @args -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and $v) {
                $maj, $min = $v.Trim().Split(".")
                if ([int]$maj -ge 3 -and [int]$min -ge 10) { return ,($parts) }
            }
        } catch {}
    }
    return $null
}

Write-Banner "Trustworthy Process Monitor (TPM) - launcher"

# 1) Python 3.10+
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    $py = Find-Python
    if ($null -eq $py) {
        Write-Host "Python 3.10+ was not found. Install it from https://www.python.org/downloads/ (tick 'Add to PATH') and run this script again." -ForegroundColor Red
        exit 1
    }
    Write-Host "Creating virtual environment with: $($py -join ' ')"
    & $py[0] @($py[1..($py.Length - 1)]) -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0) { Write-Host "venv creation failed" -ForegroundColor Red; exit 1 }
}

# 2) dependencies (only when requirements changed or first run)
$stamp = Join-Path $Root ".venv\.requirements.sha"
$reqHash = (Get-FileHash (Join-Path $Root "requirements.txt") -Algorithm SHA256).Hash
$needInstall = $true
if (Test-Path $stamp) { if ((Get-Content $stamp -Raw).Trim() -eq $reqHash) { $needInstall = $false } }
if ($needInstall) {
    Write-Host "Installing dependencies (this takes a few minutes the first time)..."
    & $venvPy -m pip install --upgrade pip --quiet --disable-pip-version-check
    & $venvPy -m pip install -r (Join-Path $Root "requirements.txt") --quiet --disable-pip-version-check --progress-bar on
    if ($LASTEXITCODE -ne 0) { Write-Host "pip install failed. Check your internet connection and re-run." -ForegroundColor Red; exit 1 }
    Set-Content -Path $stamp -Value $reqHash -Encoding ascii
    Write-Host "Dependencies installed." -ForegroundColor Green
} else { Write-Host "Dependencies up to date." }

# 3) .env
if (-not (Test-Path (Join-Path $Root ".env"))) { Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env"); Write-Host "Created .env from .env.example (defaults: no-egress profile, no API key needed)." }

# 4) Ollama (optional)
$model = "gemma4:e4b-it-qat"
try { $m = Select-String -Path (Join-Path $Root "config\settings.yaml") -Pattern '^\s*model:\s*(\S+)' | Select-Object -First 1; if ($m) { $model = $m.Matches[0].Groups[1].Value } } catch {}
$ollamaOk = $false
try { $null = & ollama --version 2>$null; if ($LASTEXITCODE -eq 0) { $ollamaOk = $true } } catch {}
if (-not $ollamaOk) { try { $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 2; if ($r.StatusCode -eq 200) { $ollamaOk = $true } } catch {} }
if ($ollamaOk) {
    $have = $false
    try { $tags = (Invoke-WebRequest -Uri "http://localhost:11434/api/tags" -UseBasicParsing -TimeoutSec 3).Content; if ($tags -match [regex]::Escape($model)) { $have = $true } } catch {}
    if ($have) { Write-Host "Ollama: local model $model is available." -ForegroundColor Green }
    else {
        Write-Host "Ollama is installed; the default model $model is not pulled. The app uses any other chat model that is installed." -ForegroundColor Yellow
        Write-Host "    Choose or download a model in the app (top bar > Local model), or run: ollama pull $model" -ForegroundColor Yellow
        if ($PullModels) { Write-Host "Pulling $model (about 6 GB)..."; & ollama pull $model }
    }
} else {
    Write-Host "Ollama not found: the app runs fully in template mode (no model-written text). Optional: the app can install Ollama and download models for you (top bar > Local model)." -ForegroundColor DarkYellow
}

# 5) doctor / demo / serve
if ($Doctor) { & $venvPy -m tpm doctor; exit $LASTEXITCODE }
if ($Demo) {
    Write-Banner "Running the demo pipeline on samples/demo_process.csv"
    & $venvPy -m tpm demo
}
$url = "http://$BindHost`:$Port"
Write-Banner "Starting the UI at $url   (workspace: $Root\workspace)   Ctrl+C stops it"
$serveArgs = @("-m", "tpm", "serve", "--host", $BindHost, "--port", "$Port")
if (-not $NoOpen) { $serveArgs += "--open" }
& $venvPy @serveArgs
exit $LASTEXITCODE
