[CmdletBinding()]
param(
    [Parameter(DontShow)]
    [string]$TestCommandAdapter
)

$ErrorActionPreference = 'Stop'
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PowerShellExe = (Get-Process -Id $PID).Path
$RepositoryPython = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $RepositoryPython -PathType Leaf) {
    $PythonExe = $RepositoryPython
}
else {
    # Fallback for an already-active compatible Python environment.
    $PythonExe = 'python'
}
$script:VerificationExitCode = 0
$locationPushed = $false

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory)]
        [string]$DisplayName,
        [Parameter(Mandatory)]
        [string]$FilePath,
        [string[]]$ArgumentList = @()
    )

    if ($TestCommandAdapter) {
        & $TestCommandAdapter $FilePath @ArgumentList
    }
    else {
        & $FilePath @ArgumentList
    }
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) {
        $exitCode = 0
    }
    if ($exitCode -ne 0) {
        $script:VerificationExitCode = $exitCode
        throw "Verification step failed ($exitCode): $DisplayName"
    }
}

try {
    if ($TestCommandAdapter -and -not (
        Test-Path -LiteralPath $TestCommandAdapter -PathType Leaf
    )) {
        throw 'The test command adapter does not exist.'
    }

    Push-Location -LiteralPath $RepositoryRoot
    $locationPushed = $true

    Invoke-CheckedCommand `
        -DisplayName 'python -m pytest tests/backend -q' `
        -FilePath $PythonExe `
        -ArgumentList @('-m', 'pytest', 'tests/backend', '-q')

    Invoke-CheckedCommand `
        -DisplayName 'python -m compileall -q src tests/backend' `
        -FilePath $PythonExe `
        -ArgumentList @('-m', 'compileall', '-q', 'src', 'tests/backend')

    # tests/work-state/run-tests.ps1 -Suite All includes the existing
    # state-document checks, so they are not invoked a second time here.
    Invoke-CheckedCommand `
        -DisplayName 'tests/work-state/run-tests.ps1 -Suite All' `
        -FilePath $PowerShellExe `
        -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            (Join-Path $RepositoryRoot 'tests/work-state/run-tests.ps1'),
            '-Suite',
            'All'
        )

    Invoke-CheckedCommand `
        -DisplayName (
            'scripts/work-state/check-public-safety.ps1 -Path . ' +
            '-Mode WorkingTree'
        ) `
        -FilePath $PowerShellExe `
        -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            (Join-Path $RepositoryRoot (
                'scripts/work-state/check-public-safety.ps1'
            )),
            '-Path',
            '.',
            '-Mode',
            'WorkingTree'
        )

    Invoke-CheckedCommand `
        -DisplayName 'git diff --check' `
        -FilePath 'git' `
        -ArgumentList @('diff', '--check')
}
catch {
    if ($script:VerificationExitCode -eq 0) {
        $script:VerificationExitCode = 1
    }
    Write-Error -ErrorAction Continue $_
}
finally {
    if ($locationPushed) {
        Pop-Location
    }
}

exit $script:VerificationExitCode
