[CmdletBinding()]
param(
    [string]$RepositoryPath = '.'
)

$ErrorActionPreference = 'Stop'

function Invoke-GitCapture {
    param([string[]]$Arguments)

    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & git -C $RepositoryPath @Arguments 2>&1 | Out-String
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorAction
    }
    [PSCustomObject]@{
        ExitCode = $exitCode
        Output = $output.Trim()
    }
}

$root = Invoke-GitCapture @('rev-parse', '--show-toplevel')
if ($root.ExitCode -ne 0) {
    Write-Error "Not a Git repository: $RepositoryPath"
    exit 2
}

$localResult = Invoke-GitCapture @('rev-parse', 'HEAD')
if ($localResult.ExitCode -ne 0) {
    Write-Error 'Unable to resolve local HEAD.'
    exit 3
}
$localHead = $localResult.Output

$upstreamResult = Invoke-GitCapture @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')
if ($upstreamResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($upstreamResult.Output)) {
    Write-Host 'Current branch has no upstream.'
    exit 4
}

$upstream = $upstreamResult.Output
$separator = $upstream.IndexOf('/')
if ($separator -lt 1 -or $separator -eq ($upstream.Length - 1)) {
    Write-Error "Unable to parse upstream: $upstream"
    exit 4
}
$remoteName = $upstream.Substring(0, $separator)
$remoteBranch = $upstream.Substring($separator + 1)

$remoteResult = Invoke-GitCapture @('ls-remote', '--exit-code', '--heads', $remoteName, "refs/heads/$remoteBranch")
if ($remoteResult.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($remoteResult.Output)) {
    Write-Error "Unable to resolve live remote branch: $upstream"
    exit 5
}

$remoteLine = @($remoteResult.Output -split "`r?`n")[0]
$remoteHead = @($remoteLine -split '\s+')[0]
if ($remoteHead -ne $localHead) {
    Write-Host "Local HEAD:  $localHead"
    Write-Host "Remote HEAD: $remoteHead"
    Write-Error 'Remote branch does not match local HEAD.'
    exit 1
}

Write-Host "Remote verification passed: $upstream = $localHead"
exit 0
