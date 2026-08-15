[CmdletBinding()]
param(
    [string]$RepositoryPath = '.',
    [switch]$Json
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

if (-not (Test-Path -LiteralPath $RepositoryPath -PathType Container)) {
    Write-Error "Repository path does not exist: $RepositoryPath"
    exit 2
}

$rootResult = Invoke-GitCapture @('rev-parse', '--show-toplevel')
if ($rootResult.ExitCode -ne 0) {
    Write-Error "Not a Git repository: $RepositoryPath"
    exit 2
}

$root = $rootResult.Output
$branchResult = Invoke-GitCapture @('symbolic-ref', '--quiet', '--short', 'HEAD')
$branch = if ($branchResult.ExitCode -eq 0) { $branchResult.Output } else { $null }

$headResult = Invoke-GitCapture @('rev-parse', 'HEAD')
$head = if ($headResult.ExitCode -eq 0) { $headResult.Output } else { $null }

$upstreamResult = Invoke-GitCapture @('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}')
$upstream = if ($upstreamResult.ExitCode -eq 0) { $upstreamResult.Output } else { $null }

$statusResult = Invoke-GitCapture @('status', '--porcelain=v1', '--untracked-files=all')
if ($statusResult.ExitCode -ne 0) {
    Write-Error 'Unable to read Git status.'
    exit 3
}
$dirty = -not [string]::IsNullOrWhiteSpace($statusResult.Output)

$ahead = $null
$behind = $null
if ($upstream) {
    $countsResult = Invoke-GitCapture @('rev-list', '--left-right', '--count', "$upstream...HEAD")
    if ($countsResult.ExitCode -eq 0) {
        $parts = @($countsResult.Output -split '\s+' | Where-Object { $_ -ne '' })
        if ($parts.Count -eq 2) {
            $behind = [int]$parts[0]
            $ahead = [int]$parts[1]
        }
    }
}

$remoteResult = Invoke-GitCapture @('remote', '-v')
$remotes = @()
if ($remoteResult.ExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace($remoteResult.Output)) {
    $remotes = @($remoteResult.Output -split "`r?`n" | Where-Object { $_ -ne '' })
}

$state = [PSCustomObject]@{
    root = $root
    branch = $branch
    head = $head
    upstream = $upstream
    dirty = $dirty
    ahead = $ahead
    behind = $behind
    remotes = $remotes
}

if ($Json) {
    $state | ConvertTo-Json -Depth 4 -Compress
}
else {
    $state | Format-List
}

exit 0
