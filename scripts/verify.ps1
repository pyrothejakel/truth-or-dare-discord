param([string]$Python = "$PSScriptRoot\..\.venv\Scripts\python.exe")
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
& $Python -m compileall -q "$projectRoot\src" "$projectRoot\tests"
if ($LASTEXITCODE -ne 0) { throw 'Compile check failed' }
& $Python -m unittest discover -s "$projectRoot\tests" -v
if ($LASTEXITCODE -ne 0) { throw 'Bot tests failed' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
