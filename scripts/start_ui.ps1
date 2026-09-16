param(
    [int]$Port = 8501
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appPath = Join-Path $projectRoot "app.py"
$ollamaPath = "C:\Users\Administrator\AppData\Local\Programs\Ollama\ollama.exe"
$modelPath = "D:\OllamaModels"
$url = "http://127.0.0.1:$Port"
$tmpDir = Join-Path $projectRoot ".tmp"
$streamlitStdout = Join-Path $tmpDir "streamlit-$Port.stdout.log"
$streamlitStderr = Join-Path $tmpDir "streamlit-$Port.stderr.log"
$streamlitPid = Join-Path $tmpDir "streamlit-$Port.pid"
$streamlitRunner = Join-Path $projectRoot "scripts\run_streamlit.cmd"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python virtual environment not found: $pythonPath"
}
if (-not (Test-Path -LiteralPath $appPath)) {
    throw "Streamlit entry point not found: $appPath"
}
if (-not (Test-Path -LiteralPath $streamlitRunner)) {
    throw "Streamlit background runner not found: $streamlitRunner"
}
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null
$env:OLLAMA_MODELS = $modelPath

function Test-OllamaApi {
    try {
        $null = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 2
        return $true
    } catch {
        return $false
    }
}

if (-not (Test-OllamaApi) -and (Test-Path -LiteralPath $ollamaPath)) {
    Write-Host "Starting Ollama..." -ForegroundColor Cyan
    Start-Process -FilePath $ollamaPath -ArgumentList "serve" -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $tmpDir "ollama-serve.stdout.log") `
        -RedirectStandardError (Join-Path $tmpDir "ollama-serve.stderr.log")
    for ($attempt = 0; $attempt -lt 30 -and -not (Test-OllamaApi); $attempt++) {
        Start-Sleep -Seconds 1
    }
}

if (-not (Test-OllamaApi)) {
    Write-Warning "Ollama is unavailable. The UI will still start; local AI features will show a reconnect hint."
} else {
    $tags = Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5
    if (-not ($tags.models.name -contains "bge-m3:latest")) {
        Write-Warning "bge-m3 was not found. Run: ollama pull bge-m3"
    }
}

try {
    $null = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
    Write-Host "PersonaX is already running. Opening $url" -ForegroundColor Green
    Start-Process $url
    exit 0
} catch {
    # The UI is not running yet, so continue with startup.
}

Write-Host "Starting PersonaX UI: $url" -ForegroundColor Green
# Launch through a detached hidden runner. Do not use -NoNewWindow here:
# closing the double-clicked console would send Ctrl+C to Streamlit.
$runnerArguments = "/d /c $streamlitRunner $Port"
Write-Host "Runner command: $env:ComSpec $runnerArguments" -ForegroundColor DarkGray
$streamlit = Start-Process -FilePath $env:ComSpec -ArgumentList $runnerArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru
$streamlit.Id | Set-Content -LiteralPath $streamlitPid -Encoding ascii

for ($attempt = 0; $attempt -lt 30; $attempt++) {
    try {
        $null = Invoke-WebRequest -Uri $url -TimeoutSec 2 -UseBasicParsing
        Start-Process $url
        Write-Host "UI opened. Streamlit is running independently (PID $($streamlit.Id))." -ForegroundColor Green
        Write-Host "Logs: $streamlitStdout / $streamlitStderr" -ForegroundColor DarkGray
        exit 0
    } catch {
        if ($streamlit.HasExited) {
            $details = ""
            if (Test-Path -LiteralPath $streamlitStderr) {
                $details = (Get-Content -LiteralPath $streamlitStderr -Tail 30) -join [Environment]::NewLine
            }
            throw "Streamlit exited during startup. Exit code: $($streamlit.ExitCode)`n$details"
        }
        Start-Sleep -Seconds 1
    }
}

Stop-Process -Id $streamlit.Id -Force -ErrorAction SilentlyContinue
throw "Streamlit did not start in 30 seconds"
