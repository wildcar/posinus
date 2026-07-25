param([string]$Python = "python")
$ErrorActionPreference = "Stop"
& $Python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
& .\.venv\Scripts\python.exe -m playwright install chromium
& .\.venv\Scripts\python.exe manage.py migrate
Write-Host "Installation complete. Create the operator with:"
Write-Host ".\.venv\Scripts\python.exe manage.py createoperator operator"

