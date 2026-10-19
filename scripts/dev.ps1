# Starts the Vantage backend (127.0.0.1:8765) and the Vite dev server (localhost:5173) together.
# Usage: ./scripts/dev.ps1        Ctrl+C stops both.
$root = Split-Path -Parent $PSScriptRoot
$backend = Start-Process -PassThru -NoNewWindow -WorkingDirectory "$root\backend" -FilePath "uv" -ArgumentList "run", "vantage"
try {
    Push-Location "$root\frontend"
    if (-not (Test-Path node_modules)) { npm install }
    npm run dev
}
finally {
    Pop-Location
    if ($backend -and -not $backend.HasExited) { Stop-Process -Id $backend.Id -Force }
}
