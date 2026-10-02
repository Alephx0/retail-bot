$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    & .\.venv\Scripts\python.exe -m patchright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Browser installation failed" }
}
& .\.venv\Scripts\python.exe -c "import mcp, patchright" 2>$null
if ($LASTEXITCODE -ne 0) {
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed" }
    & .\.venv\Scripts\python.exe -m patchright install chromium
    if ($LASTEXITCODE -ne 0) { throw "Browser installation failed" }
}
Write-Host 'Retail Desk: http://127.0.0.1:8765 (Ctrl+C to stop)'
& .\.venv\Scripts\python.exe run.py
