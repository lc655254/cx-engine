# One-shot launcher for the chaoxing automation fixture.
#
#   .\run.ps1 -Task login                # log in once, persist the session
#   .\run.ps1 -Task login -Qr            # QR-code login
#   .\run.ps1 -Task check                # verify the saved session
#   .\run.ps1 -Task probe -Chapters 1     # DOM probe, no writes to the platform
#   .\run.ps1 -Task run   -Chapters 1     # video only, first chapter
#   .\run.ps1 -Task run -Submit           # full run with quiz submission
#   .\run.ps1 -Task bank                  # which answer backend actually works
#   .\run.ps1 -Task harvest               # fill the local sqlite bank
#   .\run.ps1 -Task server                # start the local answer server
#   .\run.ps1 -Task test                  # offline test suite
param(
  [ValidateSet('login', 'check', 'probe', 'run', 'bank', 'harvest', 'server', 'test', 'status')]
  [string]$Task = 'probe',
  [int]$Chapters = 0,
  [string]$Mode = '',
  [string]$Ask = '',
  [switch]$Submit,
  [switch]$Qr,
  [switch]$Interactive
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "venv missing at $py - run: python -m venv .venv" }

function Invoke-Py {
  param([string[]]$PyArgs)
  & $py '-X' 'utf8' @PyArgs
}

switch ($Task) {
  'login' {
    $a = @((Join-Path $root 'login.py'))
    if ($Qr) { $a += '--qr' }
    if ($Interactive) { $a += '--interactive' }
    Invoke-Py $a
    break
  }
  'check' {
    Invoke-Py @((Join-Path $root 'login.py'), '--check')
    break
  }
  'harvest' {
    $a = @((Join-Path $root 'harvest.py'))
    if ($Ask) { $a += @('--ask', $Ask) }
    else { $a += @('--learn', (Join-Path $root 'logs\unanswered.csv')) }
    Invoke-Py $a
    break
  }
  'server' {
    Write-Host "starting local answer server on http://127.0.0.1:8000 (Ctrl+C to stop)" -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'bank_server.py'), '--port', '8000')
    break
  }
  'test' {
    Write-Host '>> selftest' -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'selftest.py'))
    Write-Host '>> injected JS lint' -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'check_inject.py'))
    Write-Host '>> browser + jump-to-tail smoke' -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'smoke_browser.py'))
    Write-Host '>> end-to-end quiz pipeline' -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'e2e_quiz.py'))
    Write-Host '>> end-to-end local answer server' -ForegroundColor Cyan
    Invoke-Py @((Join-Path $root 'e2e_server.py'))
    break
  }
  'bank' {
    Invoke-Py @((Join-Path $root 'probe_bank.py'))
    break
  }
  'status' {
    $cache = Join-Path $root 'evidence\bank.json'
    if (Test-Path $cache) {
      try {
        # UTF-8 explicitly: Windows PowerShell 5.1 defaults to the ANSI codepage
        # and would mangle the CJK keys in this file.
        $raw = Get-Content $cache -Raw -Encoding UTF8
        $n = ([regex]::Matches($raw, '"a"\s*:')).Count
        Write-Host "answer cache: $n entries  ($cache)"
      } catch { Write-Host "answer cache: unreadable ($cache)" }
    } else { Write-Host 'answer cache: empty' }
    try {
      $h = (Invoke-WebRequest 'http://127.0.0.1:8000/api/health' -UseBasicParsing -TimeoutSec 3).Content
      Write-Host "local answer server: $h"
    } catch { Write-Host 'local answer server: not running (run: .\run.ps1 -Task server)' }
    try {
      $cfg = Get-Content (Join-Path $root 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
      Write-Host "account: $($cfg.ACCOUNT)  course: $($cfg.COURSE_ID)  mode: $($cfg.MODE)"
    } catch { Write-Host 'config.json: unreadable' }
    break
  }
  default {
    $args = @((Join-Path $root 'cx_auto.py'))
    if ($Task -eq 'probe') { $args += '--probe' }
    if ($Task -eq 'run') { $args += '--no-quiz' }
    if ($Submit) { $args += '--quiz-submit' }
    if ($Chapters -gt 0) { $args += @('--chapters', "$Chapters") }
    if ($Mode) { $args += @('--mode', $Mode) }
    Write-Host ">> $($args -join ' ')" -ForegroundColor Cyan
    Invoke-Py $args
  }
}
