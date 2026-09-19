# Builds the Windows app and its setup program.
#   powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1
# Output: dist\NorrinTPM-Setup.exe (single file to hand out) and build\windows\dist\NorrinTPM\ (the app folder).
# Needs the project environment (.venv) with requirements.txt and pyinstaller installed.
param([switch]$SkipApp, [switch]$SkipSmokeTest)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "No .venv found. Create it first: py -3.12 -m venv .venv; .venv\Scripts\pip install -r requirements.txt pyinstaller" }
$work = Join-Path $root 'build\windows'
$appDir = Join-Path $work 'dist\NorrinTPM'
New-Item -ItemType Directory -Force $work | Out-Null
Push-Location $root
try {
    & $py -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) { & $py -m pip install --quiet 'pyinstaller>=6.10'; if ($LASTEXITCODE -ne 0) { throw 'could not install pyinstaller' } }

    Write-Host '== 1/5 icon and plotly.min.js'
    & $py packaging\windows\make_icon.py
    & $py -c "from tpm.api.server import _ensure_plotly; _ensure_plotly()"

    if (-not $SkipApp) {
        Write-Host '== 2/5 freezing the app (several minutes)'
        & $py -m PyInstaller packaging\windows\norrin_tpm.spec --noconfirm --log-level WARN --distpath (Join-Path $work 'dist') --workpath (Join-Path $work 'work')
        if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed for the app' }
    }
    if (-not (Test-Path (Join-Path $appDir 'NorrinTPM.exe'))) { throw "app folder not found: $appDir" }

    if (-not $SkipSmokeTest) {
        Write-Host '== 3/5 smoke test of the frozen app (doctor + a full analysis of the demo sample)'
        $env:TPM_DATA_DIR = Join-Path $work 'smoke_data'
        if (Test-Path $env:TPM_DATA_DIR) { Remove-Item $env:TPM_DATA_DIR -Recurse -Force }
        & (Join-Path $appDir 'NorrinTPM-cli.exe') doctor
        & (Join-Path $appDir 'NorrinTPM-cli.exe') run (Join-Path $appDir '_internal\samples\demo_process.csv') --run-id smoke --no-llm
        if ($LASTEXITCODE -ne 0) { throw 'the frozen app failed to analyse the demo sample' }
        Remove-Item Env:TPM_DATA_DIR
    }

    Write-Host '== 4/5 packing the payload'
    $zip = Join-Path $work 'app.zip'
    & $py packaging\windows\pack_payload.py $appDir $zip (Join-Path $work 'payload.json')
    if ($LASTEXITCODE -ne 0) { throw 'packing failed' }

    Write-Host '== 5/5 building the setup program'
    $sep = ';'
    $ico = Join-Path $root 'packaging\windows\norrin_tpm.ico'   # absolute: PyInstaller resolves relative paths against --specpath
    & $py -m PyInstaller packaging\windows\installer.py --noconfirm --log-level WARN --onefile --windowed --name NorrinTPM-Setup `
        --icon $ico `
        --add-data "$zip${sep}." --add-data "$(Join-Path $work 'payload.json')${sep}." --add-data "$ico${sep}." `
        --distpath (Join-Path $root 'dist') --workpath (Join-Path $work 'work_setup') --specpath (Join-Path $work 'spec_setup') `
        --exclude-module numpy --exclude-module pandas --exclude-module scipy --exclude-module sklearn
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed for the setup program' }
    $setup = Join-Path $root 'dist\NorrinTPM-Setup.exe'
    Write-Host ("Done: {0} ({1:N0} MB)" -f $setup, ((Get-Item $setup).Length / 1MB))
}
finally { Pop-Location }
