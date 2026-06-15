$ErrorActionPreference = "Stop"

$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($python) {
    $pythonExe = $python.Source
} else {
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if (!$py) {
        throw "Python 3.12+ is required. Install Python or use Docker Compose."
    }
    $pythonExe = $py.Source
}

if (!(Test-Path ".venv")) {
    & $pythonExe -m venv .venv
}

.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:APP_SECRET = "dev-secret"
$env:DATABASE_PATH = "$PWD\data\dev.sqlite3"
$env:ADMIN_USERNAME = "admin@example.com"
$env:ADMIN_PASSWORD = "admin-dev-password"
.\.venv\Scripts\python.exe -m flask --app "app:create_app()" run --host 127.0.0.1 --port 8080 --debug
