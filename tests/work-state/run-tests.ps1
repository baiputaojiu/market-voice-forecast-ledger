[CmdletBinding()]
param(
    [ValidateSet('All', 'Docs', 'Scripts', 'PublicSafety', 'Integration', 'SaveSkill', 'ResumeSkill')]
    [string]$Suite = 'All'
)

$ErrorActionPreference = 'Stop'
$script:Failures = 0
$script:Passes = 0
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Assert-True {
    param(
        [Parameter(Mandatory)]
        [bool]$Condition,
        [Parameter(Mandatory)]
        [string]$Message
    );

    if ($Condition) {
        $script:Passes++
        Write-Host "PASS: $Message"
        return
    }

    $script:Failures++
    Write-Host "FAIL: $Message" -ForegroundColor Red
}

function Assert-FileContainsHeadings {
    param(
        [Parameter(Mandatory)]
        [string]$RelativePath,
        [Parameter(Mandatory)]
        [string[]]$Headings
    );

    $path = Join-Path $ProjectRoot $RelativePath
    $exists = Test-Path -LiteralPath $path -PathType Leaf
    Assert-True $exists "$RelativePath exists"
    if (-not $exists) {
        return
    }

    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $path
    foreach ($heading in $Headings) {
        Assert-True ($content -match [regex]::Escape($heading)) "$RelativePath contains heading '$heading'"
    }
}

function Test-Docs {
    $requiredFiles = @(
        'AGENTS.md',
        'README.md',
        '.gitignore',
        '.gitattributes',
        '.editorconfig',
        'docs/project/requirements.md',
        'docs/project/decisions.md',
        'docs/project/plan.md',
        'docs/project/status.md',
        'docs/project/public-data-policy.md',
        'tests/work-state/README.md',
        'tests/work-state/skill-evaluation.md'
    );

    foreach ($relativePath in $requiredFiles) {
        Assert-True (Test-Path -LiteralPath (Join-Path $ProjectRoot $relativePath) -PathType Leaf) "$relativePath exists"
    }

    Assert-FileContainsHeadings -RelativePath 'docs/project/requirements.md' -Headings @(
        'Project Purpose',
        'Current Scope',
        'Confirmed Requirements',
        'Analysis Information Boundary',
        'Future Features Outside MVP'
    );
    Assert-FileContainsHeadings -RelativePath 'docs/project/decisions.md' -Headings @(
        'Recording Rules',
        'Accepted Decisions',
        'Rejected or Superseded Options'
    );
    Assert-FileContainsHeadings -RelativePath 'docs/project/plan.md' -Headings @(
        'Current Milestone',
        'Completed',
        'In Progress',
        'Not Started'
    );
    Assert-FileContainsHeadings -RelativePath 'docs/project/status.md' -Headings @(
        'Current Phase',
        'Git State',
        'Completed',
        'In Progress',
        'Verification Results',
        'Known Issues',
        'Open Questions',
        'Next Actions',
        'Important Files'
    );
    Assert-FileContainsHeadings -RelativePath 'docs/project/public-data-policy.md' -Headings @(
        'Public Information',
        'Excluded Information',
        'Default Local Data Location'
    );
    Assert-FileContainsHeadings -RelativePath 'tests/work-state/README.md' -Headings @(
        'Fast Tests',
        'Skill Evaluations'
    );
    Assert-FileContainsHeadings -RelativePath 'tests/work-state/skill-evaluation.md' -Headings @(
        'Save Skill',
        'Resume Skill',
        'Stable Result'
    );

    $agentsPath = Join-Path $ProjectRoot 'AGENTS.md'
    if (Test-Path -LiteralPath $agentsPath -PathType Leaf) {
        $agentsLines = @(Get-Content -Encoding UTF8 -LiteralPath $agentsPath)
        Assert-True ($agentsLines.Count -le 60) 'AGENTS.md stays at or below 60 lines'
        $agentsContent = $agentsLines -join "`n"
        Assert-True ($agentsContent -match '\$save-work-state') 'AGENTS.md routes save requests to $save-work-state'
        Assert-True ($agentsContent -match '\$resume-work-state') 'AGENTS.md routes resume requests to $resume-work-state'
    }

    $readmePath = Join-Path $ProjectRoot 'README.md'
    if (Test-Path -LiteralPath $readmePath -PathType Leaf) {
        $readme = Get-Content -Raw -Encoding UTF8 -LiteralPath $readmePath
        foreach ($relativeLink in @(
            'docs/project/status.md',
            'docs/project/requirements.md',
            'docs/project/decisions.md',
            'docs/project/plan.md'
        )) {
            Assert-True ($readme -match [regex]::Escape($relativeLink)) "README.md links to $relativeLink"
        }
    }
}

function Invoke-ScriptProcess {
    param(
        [Parameter(Mandatory)]
        [string]$ScriptPath,
        [string[]]$Arguments = @()
    )

    $powerShellExe = (Get-Process -Id $PID).Path
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & $powerShellExe -NoProfile -ExecutionPolicy Bypass -File $ScriptPath @Arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    [PSCustomObject]@{
        ExitCode = $exitCode
        Output = $output.Trim()
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory)]
        [string[]]$Arguments
    )

    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & git -C $WorkingDirectory @Arguments 2>&1 | Out-String
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($exitCode -ne 0) {
        throw "git failed in $WorkingDirectory`: git $($Arguments -join ' ')`n$output"
    }
    $output.Trim()
}

function Remove-TestRoot {
    param([Parameter(Mandatory)][string]$Path)

    $fullPath = [System.IO.Path]::GetFullPath($Path)
    $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
    $leaf = Split-Path -Leaf $fullPath
    if (-not $fullPath.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove non-temp path: $fullPath"
    }
    if (-not $leaf.StartsWith('mvfl-work-state-tests-', [System.StringComparison]::Ordinal)) {
        throw "Refusing to remove unexpected temp directory: $fullPath"
    }
    if (Test-Path -LiteralPath $fullPath) {
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}

function Test-Scripts {
    $scriptPaths = @{
        Inspect = Join-Path $ProjectRoot 'scripts/work-state/inspect-git-state.ps1'
        Safety = Join-Path $ProjectRoot 'scripts/work-state/check-public-safety.ps1'
        Docs = Join-Path $ProjectRoot 'scripts/work-state/check-state-docs.ps1'
        Remote = Join-Path $ProjectRoot 'scripts/work-state/verify-remote-head.ps1'
    }

    foreach ($name in $scriptPaths.Keys) {
        Assert-True (Test-Path -LiteralPath $scriptPaths[$name] -PathType Leaf) "$name script exists"
    }
    if (@($scriptPaths.Values | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -gt 0) {
        return
    }

    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mvfl-work-state-tests-" + [guid]::NewGuid().ToString('N'))
    $nonGit = Join-Path $testRoot 'not-a-repository'
    $safeData = Join-Path $testRoot 'safe-data'
    $source = Join-Path $testRoot 'source'
    $remote = Join-Path $testRoot 'remote.git'

    try {
        New-Item -ItemType Directory -Path $nonGit, $safeData, $source -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $safeData 'README.md') -Encoding ASCII -Value '# Safe fixture'

        $notRepo = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $nonGit, '-Json')
        Assert-True ($notRepo.ExitCode -ne 0) 'inspect-git-state rejects a non-Git directory'

        $safeResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $safeData, '-MaxFileBytes', '1024')
        Assert-True ($safeResult.ExitCode -eq 0) 'check-public-safety accepts a safe text fixture'

        $secretFixture = 'api' + '_key = "real-looking-secret-value-1234567890"'
        Set-Content -LiteralPath (Join-Path $safeData 'secret.txt') -Encoding ASCII -Value $secretFixture
        $secretResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $safeData, '-MaxFileBytes', '1024')
        Assert-True ($secretResult.ExitCode -ne 0) 'check-public-safety rejects a secret assignment'
        Remove-Item -LiteralPath (Join-Path $safeData 'secret.txt') -Force

        Set-Content -LiteralPath (Join-Path $safeData 'production.sqlite') -Encoding ASCII -Value 'not a real database'
        $databaseResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $safeData, '-MaxFileBytes', '1024')
        Assert-True ($databaseResult.ExitCode -ne 0) 'check-public-safety rejects a database file'
        Remove-Item -LiteralPath (Join-Path $safeData 'production.sqlite') -Force

        Set-Content -LiteralPath (Join-Path $safeData 'large.txt') -Encoding ASCII -Value ('x' * 256)
        $largeResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $safeData, '-MaxFileBytes', '100')
        Assert-True ($largeResult.ExitCode -ne 0) 'check-public-safety rejects a file above the configured size limit'

        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & git init --bare $remote 2>&1 | Out-Null
        $bareExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($bareExitCode -ne 0) { throw 'git init --bare failed' }
        Invoke-Git -WorkingDirectory $source -Arguments @('init') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('branch', '-M', 'main') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('config', 'user.name', 'Work State Tests') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('config', 'user.email', 'work-state-tests@example.invalid') | Out-Null
        Set-Content -LiteralPath (Join-Path $source 'README.md') -Encoding ASCII -Value '# Fixture repository'
        Invoke-Git -WorkingDirectory $source -Arguments @('add', 'README.md') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('commit', '-m', 'initial fixture') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('remote', 'add', 'origin', $remote) | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('push', '-u', 'origin', 'main') | Out-Null

        $fixtureHead = Invoke-Git -WorkingDirectory $source -Arguments @('rev-parse', 'HEAD')
        Invoke-Git -WorkingDirectory $source -Arguments @('branch', '--unset-upstream') | Out-Null

        $noUpstreamInspect = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $source, '-Json')
        $noUpstreamState = if ($noUpstreamInspect.ExitCode -eq 0) {
            $noUpstreamInspect.Output | ConvertFrom-Json
        }
        else {
            $null
        }
        Assert-True ($noUpstreamInspect.ExitCode -eq 0 -and $null -ne $noUpstreamState) 'inspect-git-state emits valid JSON without an upstream'
        if ($null -ne $noUpstreamState) {
            Assert-True ($noUpstreamState.branch -eq 'main') 'inspect-git-state preserves the branch without an upstream'
            Assert-True ($noUpstreamState.head -eq $fixtureHead) 'inspect-git-state preserves HEAD without an upstream'
            Assert-True ($null -eq $noUpstreamState.upstream -and $null -eq $noUpstreamState.ahead -and $null -eq $noUpstreamState.behind) 'inspect-git-state reports null upstream counts without an upstream'
        }
        Assert-True ($noUpstreamInspect.Output -notmatch '(?i)fatal|NativeCommandError') 'inspect-git-state suppresses raw expected Git errors without an upstream'

        $noUpstreamVerify = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $source)
        Assert-True ($noUpstreamVerify.ExitCode -eq 4) 'verify-remote-head returns exit 4 without an upstream'
        Assert-True ($noUpstreamVerify.Output -match 'Current branch has no upstream\.') 'verify-remote-head reports the controlled no-upstream message'
        Assert-True ($noUpstreamVerify.Output -notmatch '(?i)fatal|NativeCommandError') 'verify-remote-head suppresses raw expected Git errors without an upstream'

        Invoke-Git -WorkingDirectory $source -Arguments @('checkout', '--detach', $fixtureHead) | Out-Null
        $detachedInspect = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $source, '-Json')
        $detachedState = if ($detachedInspect.ExitCode -eq 0) {
            $detachedInspect.Output | ConvertFrom-Json
        }
        else {
            $null
        }
        Assert-True ($detachedInspect.ExitCode -eq 0 -and $null -ne $detachedState) 'inspect-git-state emits valid JSON for detached HEAD'
        if ($null -ne $detachedState) {
            Assert-True ($null -eq $detachedState.branch) 'inspect-git-state reports a null branch for detached HEAD'
            Assert-True ($detachedState.head -eq $fixtureHead) 'inspect-git-state preserves detached HEAD'
            Assert-True ($null -eq $detachedState.upstream -and $null -eq $detachedState.ahead -and $null -eq $detachedState.behind) 'inspect-git-state reports null upstream counts for detached HEAD'
        }
        Assert-True ($detachedInspect.Output -notmatch '(?i)fatal|NativeCommandError') 'inspect-git-state suppresses raw expected Git errors for detached HEAD'

        $detachedVerify = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $source)
        Assert-True ($detachedVerify.ExitCode -eq 4) 'verify-remote-head returns exit 4 for detached HEAD'
        Assert-True ($detachedVerify.Output -match 'Current branch has no upstream\.') 'verify-remote-head reports the controlled detached-HEAD message'
        Assert-True ($detachedVerify.Output -notmatch '(?i)fatal|NativeCommandError') 'verify-remote-head suppresses raw expected Git errors for detached HEAD'

        Invoke-Git -WorkingDirectory $source -Arguments @('checkout', 'main') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('branch', '--set-upstream-to', 'origin/main', 'main') | Out-Null

        $cleanState = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $source, '-Json')
        Assert-True ($cleanState.ExitCode -eq 0) 'inspect-git-state accepts a Git repository'
        if ($cleanState.ExitCode -eq 0) {
            $state = $cleanState.Output | ConvertFrom-Json
            Assert-True ($state.branch -eq 'main') 'inspect-git-state reports the current branch'
            Assert-True (-not $state.dirty) 'inspect-git-state reports a clean tree'
            Assert-True ($state.upstream -eq 'origin/main') 'inspect-git-state reports upstream'
            Assert-True ($state.ahead -eq 0 -and $state.behind -eq 0) 'inspect-git-state reports zero ahead and behind'
        }

        Set-Content -LiteralPath (Join-Path $source 'untracked.txt') -Encoding ASCII -Value 'untracked'
        $dirtyState = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $source, '-Json')
        Assert-True ($dirtyState.ExitCode -eq 0 -and (($dirtyState.Output | ConvertFrom-Json).dirty)) 'inspect-git-state includes untracked files in dirty state'
        Remove-Item -LiteralPath (Join-Path $source 'untracked.txt') -Force

        $remoteMatch = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $source)
        Assert-True ($remoteMatch.ExitCode -eq 0) 'verify-remote-head succeeds when remote and local HEAD match'

        Set-Content -LiteralPath (Join-Path $source 'local-only.txt') -Encoding ASCII -Value 'not pushed'
        Invoke-Git -WorkingDirectory $source -Arguments @('add', 'local-only.txt') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('commit', '-m', 'local only') | Out-Null
        $remoteMismatch = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $source)
        Assert-True ($remoteMismatch.ExitCode -ne 0) 'verify-remote-head rejects an unpushed local commit'

        $docsResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Docs -Arguments @('-ProjectRoot', $ProjectRoot)
        Assert-True ($docsResult.ExitCode -eq 0) 'check-state-docs accepts the project state documents'
    }
    finally {
        Remove-TestRoot -Path $testRoot
    }
}

function Test-PublicSafety {
    $safetyScript = Join-Path $ProjectRoot 'scripts/work-state/check-public-safety.ps1'
    if (-not (Test-Path -LiteralPath $safetyScript -PathType Leaf)) {
        Assert-True $false 'PublicSafety prerequisite Safety script exists'
        return
    }

    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mvfl-work-state-tests-" + [guid]::NewGuid().ToString('N'))
    $stagedSafety = Join-Path $testRoot 'staged-safety'
    $secretFixture = 'api' + '_key = "real-looking-secret-value-1234567890"'

    try {
        New-Item -ItemType Directory -Path $stagedSafety -Force | Out-Null
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('init') | Out-Null
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('config', 'user.name', 'Work State Tests') | Out-Null
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('config', 'user.email', 'work-state-tests@example.invalid') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'README.md') -Encoding ASCII -Value '# Staged safety fixture'
        Set-Content -LiteralPath (Join-Path $stagedSafety '.gitignore') -Encoding ASCII -Value @(
            '*.pem', '*.pyc', '*.pyo', '*.pyd', '.coverage', '.coverage.*',
            '*.log', '*.tmp', '*.bak', '*.part', '.mypy_cache/', '.ruff_cache/'
        )
        Set-Content -LiteralPath (Join-Path $stagedSafety 'deleted-secret.txt') -Encoding ASCII -Value $secretFixture
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', 'README.md', '.gitignore', 'deleted-secret.txt') | Out-Null
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('commit', '-m', 'staged safety baseline') | Out-Null

        Set-Content -LiteralPath (Join-Path $stagedSafety 'index-secret.txt') -Encoding ASCII -Value $secretFixture
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', 'index-secret.txt') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'index-secret.txt') -Encoding ASCII -Value 'safe working copy'
        $stagedSecret = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($stagedSecret.ExitCode -eq 1) 'staged safety reads secret content from the index instead of the working tree'
        Assert-True ($stagedSecret.Output -notmatch [regex]::Escape($secretFixture)) 'staged secret failure does not print secret content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'index-secret.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'index-secret.txt') -Force

        Set-Content -LiteralPath (Join-Path $stagedSafety 'deleted-working-copy.pem') -Encoding ASCII -Value 'synthetic certificate body'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '-f', '--', 'deleted-working-copy.pem') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'deleted-working-copy.pem') -Force
        $missingWorkingFile = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($missingWorkingFile.ExitCode -eq 1) 'staged safety rejects a forbidden staged file missing from the working tree'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'deleted-working-copy.pem') | Out-Null

        Set-Content -LiteralPath (Join-Path $stagedSafety 'safe-index.txt') -Encoding ASCII -Value 'safe staged content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', 'safe-index.txt') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'safe-index.txt') -Encoding ASCII -Value $secretFixture
        $workingSecret = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($workingSecret.ExitCode -eq 0) 'staged safety ignores unsafe content present only in the working tree'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'safe-index.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'safe-index.txt') -Force

        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('rm', '--', 'deleted-secret.txt') | Out-Null
        $stagedDeletion = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($stagedDeletion.ExitCode -eq 0) 'staged safety does not inspect a staged deletion as committed content'
        Assert-True ($stagedDeletion.Output -notmatch [regex]::Escape($secretFixture)) 'staged deletion does not print deleted content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'deleted-secret.txt') | Out-Null
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--', 'deleted-secret.txt') | Out-Null

        $unicodeRelativePath = 'space directory/non-ASCII-日本語.txt'
        New-Item -ItemType Directory -Path (Join-Path $stagedSafety 'space directory') -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety $unicodeRelativePath) -Encoding UTF8 -Value $secretFixture
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', $unicodeRelativePath) | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety $unicodeRelativePath) -Encoding UTF8 -Value 'safe working copy'
        $unicodeStagedSecret = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($unicodeStagedSecret.ExitCode -eq 1) 'staged safety reads an index path containing spaces and non-ASCII characters'
        Assert-True ($unicodeStagedSecret.Output -match [regex]::Escape("VIOLATION: Possible secret detected: $unicodeRelativePath")) 'staged safety reports the decoded non-ASCII index path safely'
        Assert-True ($unicodeStagedSecret.Output -notmatch 'MethodInvocationException|Illegal characters in path|FullyQualifiedErrorId|NativeCommandError|fatal:') 'staged non-ASCII path failure suppresses raw tool errors'
        Assert-True ($unicodeStagedSecret.Output -notmatch [regex]::Escape($secretFixture)) 'staged non-ASCII path failure does not print secret content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', $unicodeRelativePath) | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety $unicodeRelativePath) -Force

        Set-Content -LiteralPath (Join-Path $stagedSafety 'utf16-index.txt') -Encoding Unicode -Value $secretFixture
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', 'utf16-index.txt') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'utf16-index.txt') -Encoding Unicode -Value 'safe working copy'
        $utf16StagedSecret = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($utf16StagedSecret.ExitCode -eq 1) 'staged safety decodes BOM-marked index text before checking for secrets'
        Assert-True ($utf16StagedSecret.Output -notmatch [regex]::Escape($secretFixture)) 'staged UTF-16 secret failure does not print secret content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'utf16-index.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'utf16-index.txt') -Force

        $newDeniedPaths = @(
            'certificate.pem',
            'artifact.pyc',
            'artifact.pyo',
            'artifact.pyd',
            '.coverage',
            '.coverage.synthetic',
            'run.log',
            'scratch.tmp',
            'backup.bak',
            'download.part',
            '.mypy_cache/cache.txt',
            '.ruff_cache/cache.txt',
            'ledger.db-wal',
            'ledger.sqlite-journal',
            'ledger.sqlite3-wal',
            '.pytest_cache/cache.txt',
            '.idea/workspace.xml',
            '.vscode/settings.json',
            'editor.user',
            'solution.suo',
            '.DS_Store',
            'Thumbs.db',
            'desktop.ini',
            '.worktrees/private.txt'
        )
        foreach ($deniedPath in $newDeniedPaths) {
            $absoluteDeniedPath = Join-Path $stagedSafety ($deniedPath -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            New-Item -ItemType Directory -Path (Split-Path -Parent $absoluteDeniedPath) -Force | Out-Null
            Set-Content -LiteralPath $absoluteDeniedPath -Encoding ASCII -Value 'content independent denylist fixture'
            Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '-f', '--', $deniedPath) | Out-Null
            $deniedResult = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
            Assert-True ($deniedResult.ExitCode -eq 1) "staged safety rejects force-staged denied path $deniedPath"
            Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', $deniedPath) | Out-Null
            Remove-Item -LiteralPath $absoluteDeniedPath -Force
        }

        $invalidUtf8Path = Join-Path $stagedSafety 'invalid-utf8.txt'
        [System.IO.File]::WriteAllBytes($invalidUtf8Path, [byte[]](0xC3, 0x28))
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', 'invalid-utf8.txt') | Out-Null
        Set-Content -LiteralPath $invalidUtf8Path -Encoding ASCII -Value 'safe working copy'
        $invalidUtf8 = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($invalidUtf8.ExitCode -eq 1) 'staged safety fails closed when index text cannot be decoded'
        Assert-True ($invalidUtf8.Output -match [regex]::Escape('VIOLATION: Unable to inspect staged file content: invalid-utf8.txt')) 'staged decode failure uses a stable safe message'
        Assert-True ($invalidUtf8.Output -notmatch 'DecoderFallbackException|MethodInvocationException|FullyQualifiedErrorId|NativeCommandError|fatal:') 'staged decode failure suppresses raw tool errors'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'invalid-utf8.txt') | Out-Null
        Remove-Item -LiteralPath $invalidUtf8Path -Force

        Set-Content -LiteralPath (Join-Path $stagedSafety 'staged-large.txt') -Encoding ASCII -Value ('x' * 256)
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', 'staged-large.txt') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'staged-large.txt') -Encoding ASCII -Value 'small working copy'
        $stagedLarge = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged', '-MaxFileBytes', '100')
        Assert-True ($stagedLarge.ExitCode -eq 1 -and $stagedLarge.Output -match 'File exceeds 100 bytes: staged-large\.txt') 'staged safety applies the size limit to index bytes'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'staged-large.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'staged-large.txt') -Force

        Set-Content -LiteralPath (Join-Path $stagedSafety 'working-large.txt') -Encoding ASCII -Value 'small staged content'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', 'working-large.txt') | Out-Null
        Set-Content -LiteralPath (Join-Path $stagedSafety 'working-large.txt') -Encoding ASCII -Value ('x' * 256)
        $workingLarge = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged', '-MaxFileBytes', '100')
        Assert-True ($workingLarge.ExitCode -eq 0) 'staged safety ignores an oversized working copy when index content is small'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('restore', '--staged', '--', 'working-large.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $stagedSafety 'working-large.txt') -Force

        Set-Content -LiteralPath (Join-Path $stagedSafety 'missing-index-object.txt') -Encoding ASCII -Value 'unique staged object for fail-closed lookup'
        Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('add', '--', 'missing-index-object.txt') | Out-Null
        $missingObjectId = Invoke-Git -WorkingDirectory $stagedSafety -Arguments @('rev-parse', ':missing-index-object.txt')
        $missingObjectPath = Join-Path $stagedSafety ".git/objects/$($missingObjectId.Substring(0, 2))/$($missingObjectId.Substring(2))"
        Assert-True (Test-Path -LiteralPath $missingObjectPath -PathType Leaf) 'staged lookup failure fixture owns a loose index object'
        Remove-Item -LiteralPath $missingObjectPath -Force
        $missingObject = Invoke-ScriptProcess -ScriptPath $safetyScript -Arguments @('-Path', $stagedSafety, '-Mode', 'Staged')
        Assert-True ($missingObject.ExitCode -eq 1) 'staged safety fails closed when an index blob cannot be read'
        Assert-True ($missingObject.Output -match [regex]::Escape('VIOLATION: Unable to inspect staged file content: missing-index-object.txt')) 'staged blob lookup failure uses a stable safe message'
        Assert-True ($missingObject.Output -notmatch [regex]::Escape($missingObjectId)) 'staged blob lookup failure does not print the object ID'
        Assert-True ($missingObject.Output -notmatch 'NativeCommandError|fatal:|could not read') 'staged blob lookup failure suppresses raw Git errors'
    }
    finally {
        Remove-TestRoot -Path $testRoot
    }
}

function Test-Integration {
    $scriptPaths = @{
        Inspect = Join-Path $ProjectRoot 'scripts/work-state/inspect-git-state.ps1'
        Safety = Join-Path $ProjectRoot 'scripts/work-state/check-public-safety.ps1'
        Remote = Join-Path $ProjectRoot 'scripts/work-state/verify-remote-head.ps1'
    }

    foreach ($name in $scriptPaths.Keys) {
        Assert-True (Test-Path -LiteralPath $scriptPaths[$name] -PathType Leaf) "Integration prerequisite $name exists"
    }
    if (@($scriptPaths.Values | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) }).Count -gt 0) {
        return
    }

    $testRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("mvfl-work-state-tests-" + [guid]::NewGuid().ToString('N'))
    $source = Join-Path $testRoot 'source'
    $consumer = Join-Path $testRoot 'consumer'
    $remote = Join-Path $testRoot 'remote.git'

    try {
        New-Item -ItemType Directory -Path $source -Force | Out-Null

        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & git init --bare $remote 2>&1 | Out-Null
        $bareExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($bareExitCode -ne 0) { throw 'git init --bare failed' }

        Invoke-Git -WorkingDirectory $source -Arguments @('init') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('branch', '-M', 'main') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('config', 'user.name', 'Work State Tests') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('config', 'user.email', 'work-state-tests@example.invalid') | Out-Null
        Set-Content -LiteralPath (Join-Path $source 'README.md') -Encoding ASCII -Value '# Integration fixture'
        Invoke-Git -WorkingDirectory $source -Arguments @('add', 'README.md') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('commit', '-m', 'initial fixture') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('remote', 'add', 'origin', $remote) | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('push', '-u', 'origin', 'main') | Out-Null
        Invoke-Git -WorkingDirectory $remote -Arguments @('symbolic-ref', 'HEAD', 'refs/heads/main') | Out-Null
        Invoke-Git -WorkingDirectory $testRoot -Arguments @('clone', '--branch', 'main', $remote, $consumer) | Out-Null
        Invoke-Git -WorkingDirectory $consumer -Arguments @('config', 'user.name', 'Work State Tests') | Out-Null
        Invoke-Git -WorkingDirectory $consumer -Arguments @('config', 'user.email', 'work-state-tests@example.invalid') | Out-Null

        Set-Content -LiteralPath (Join-Path $source 'remote-change.txt') -Encoding ASCII -Value 'remote change'
        Invoke-Git -WorkingDirectory $source -Arguments @('add', 'remote-change.txt') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('commit', '-m', 'remote change') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('push') | Out-Null
        Invoke-Git -WorkingDirectory $consumer -Arguments @('fetch', '--prune') | Out-Null

        $behindResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $consumer, '-Json')
        $behindState = if ($behindResult.ExitCode -eq 0) { $behindResult.Output | ConvertFrom-Json } else { $null }
        Assert-True ($behindResult.ExitCode -eq 0 -and $behindState.ahead -eq 0 -and $behindState.behind -eq 1) 'second clone detects one remote commit behind'

        Invoke-Git -WorkingDirectory $consumer -Arguments @('pull', '--ff-only') | Out-Null
        $fastForwardVerify = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $consumer)
        Assert-True ($fastForwardVerify.ExitCode -eq 0) 'fast-forwarded second clone matches the live remote head'

        Set-Content -LiteralPath (Join-Path $consumer 'machine-local.txt') -Encoding ASCII -Value 'local only'
        $dirtyResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $consumer, '-Json')
        Assert-True ($dirtyResult.ExitCode -eq 0 -and (($dirtyResult.Output | ConvertFrom-Json).dirty)) 'second clone detects an untracked machine-local edit'
        Remove-Item -LiteralPath (Join-Path $consumer 'machine-local.txt') -Force

        Set-Content -LiteralPath (Join-Path $consumer 'production.sqlite') -Encoding ASCII -Value 'not a real database'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('add', '-f', 'production.sqlite') | Out-Null
        $stagedDatabase = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $consumer, '-Mode', 'Staged')
        Assert-True ($stagedDatabase.ExitCode -ne 0) 'staged safety scan rejects a force-added database'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('restore', '--staged', 'production.sqlite') | Out-Null
        Remove-Item -LiteralPath (Join-Path $consumer 'production.sqlite') -Force

        $credentialsDirectory = Join-Path $consumer 'credentials'
        New-Item -ItemType Directory -Path $credentialsDirectory -Force | Out-Null
        Set-Content -LiteralPath (Join-Path $credentialsDirectory 'note.txt') -Encoding ASCII -Value 'machine-specific credential material'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('add', '-f', 'credentials/note.txt') | Out-Null
        $stagedCredentials = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $consumer, '-Mode', 'Staged')
        Assert-True ($stagedCredentials.ExitCode -ne 0) 'staged safety scan rejects a force-added credentials directory'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('restore', '--staged', 'credentials/note.txt') | Out-Null
        Remove-Item -LiteralPath $credentialsDirectory -Recurse -Force

        $secretFixture = 'api' + '_key = "real-looking-secret-value-1234567890"'
        Set-Content -LiteralPath (Join-Path $consumer 'local-secret.txt') -Encoding ASCII -Value $secretFixture
        Invoke-Git -WorkingDirectory $consumer -Arguments @('add', 'local-secret.txt') | Out-Null
        $stagedSecret = Invoke-ScriptProcess -ScriptPath $scriptPaths.Safety -Arguments @('-Path', $consumer, '-Mode', 'Staged')
        Assert-True ($stagedSecret.ExitCode -ne 0) 'staged safety scan rejects a secret assignment'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('restore', '--staged', 'local-secret.txt') | Out-Null
        Remove-Item -LiteralPath (Join-Path $consumer 'local-secret.txt') -Force

        Set-Content -LiteralPath (Join-Path $consumer 'consumer-change.txt') -Encoding ASCII -Value 'consumer change'
        Invoke-Git -WorkingDirectory $consumer -Arguments @('add', 'consumer-change.txt') | Out-Null
        Invoke-Git -WorkingDirectory $consumer -Arguments @('commit', '-m', 'consumer-only change') | Out-Null
        Set-Content -LiteralPath (Join-Path $source 'second-remote-change.txt') -Encoding ASCII -Value 'second remote change'
        Invoke-Git -WorkingDirectory $source -Arguments @('add', 'second-remote-change.txt') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('commit', '-m', 'second remote change') | Out-Null
        Invoke-Git -WorkingDirectory $source -Arguments @('push') | Out-Null
        Invoke-Git -WorkingDirectory $consumer -Arguments @('fetch', '--prune') | Out-Null

        $divergedResult = Invoke-ScriptProcess -ScriptPath $scriptPaths.Inspect -Arguments @('-RepositoryPath', $consumer, '-Json')
        $divergedState = if ($divergedResult.ExitCode -eq 0) { $divergedResult.Output | ConvertFrom-Json } else { $null }
        Assert-True ($divergedResult.ExitCode -eq 0 -and $divergedState.ahead -eq 1 -and $divergedState.behind -eq 1) 'second clone detects divergent local and remote history'

        $divergedVerify = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $consumer)
        Assert-True ($divergedVerify.ExitCode -ne 0) 'remote verification rejects a divergent local head'

        Invoke-Git -WorkingDirectory $consumer -Arguments @('remote', 'set-url', 'origin', (Join-Path $testRoot 'missing-remote.git')) | Out-Null
        $missingRemoteVerify = Invoke-ScriptProcess -ScriptPath $scriptPaths.Remote -Arguments @('-RepositoryPath', $consumer)
        Assert-True ($missingRemoteVerify.ExitCode -ne 0) 'remote verification rejects an unreachable remote'
    }
    finally {
        Remove-TestRoot -Path $testRoot
    }
}

function Test-SaveSkill {
    $skillRoot = Join-Path $ProjectRoot '.agents/skills/save-work-state'
    $skillPath = Join-Path $skillRoot 'SKILL.md'
    $metadataPath = Join-Path $skillRoot 'agents/openai.yaml'
    $scenarioPath = Join-Path $ProjectRoot 'tests/work-state/scenarios/save-work-state.md'

    Assert-True (Test-Path -LiteralPath $skillPath -PathType Leaf) 'save-work-state SKILL.md exists'
    Assert-True (Test-Path -LiteralPath $metadataPath -PathType Leaf) 'save-work-state openai.yaml exists'
    Assert-True (Test-Path -LiteralPath $scenarioPath -PathType Leaf) 'save-work-state scenario exists'
    if (-not (Test-Path -LiteralPath $skillPath -PathType Leaf)) {
        return
    }

    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $skillPath
    Assert-True ($content -match '(?m)^name: save-work-state$') 'save-work-state has the expected name'
    Assert-True ($content -match '(?m)^description: Use when ') 'save-work-state description starts with Use when'
    Assert-True ($content -notmatch 'TODO|TBD') 'save-work-state has no placeholders'
    foreach ($scriptReference in @(
        'scripts/work-state/inspect-git-state.ps1',
        'scripts/work-state/check-state-docs.ps1',
        'scripts/work-state/check-public-safety.ps1',
        'scripts/work-state/verify-remote-head.ps1'
    )) {
        Assert-True ($content -match [regex]::Escape($scriptReference)) "save-work-state references $scriptReference"
    }
    Assert-True ($content -match [regex]::Escape('git add .')) 'save-work-state explicitly addresses whole-tree staging'
    Assert-True ($content -match 'live remote branch SHA') 'save-work-state requires live remote verification'
    Assert-True ($content -match 'cross-PC save incomplete') 'save-work-state defines incomplete save reporting'

    if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
        $metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $metadataPath
        Assert-True ($metadata -match [regex]::Escape('$save-work-state')) 'save-work-state metadata default prompt invokes the skill'
        Assert-True ($metadata -match '(?m)^\s*allow_implicit_invocation: true$') 'save-work-state allows implicit invocation'
    }

    if (Test-Path -LiteralPath $scenarioPath -PathType Leaf) {
        $scenario = Get-Content -Raw -Encoding UTF8 -LiteralPath $scenarioPath
        Assert-True ($scenario -match '## Prompt') 'save-work-state scenario has a prompt'
        Assert-True ($scenario -match '## Evaluation contract') 'save-work-state scenario has an evaluation contract'
    }
}

function Test-ResumeSkill {
    $skillRoot = Join-Path $ProjectRoot '.agents/skills/resume-work-state'
    $skillPath = Join-Path $skillRoot 'SKILL.md'
    $metadataPath = Join-Path $skillRoot 'agents/openai.yaml'
    $scenarioPath = Join-Path $ProjectRoot 'tests/work-state/scenarios/resume-work-state.md'

    Assert-True (Test-Path -LiteralPath $skillPath -PathType Leaf) 'resume-work-state SKILL.md exists'
    Assert-True (Test-Path -LiteralPath $metadataPath -PathType Leaf) 'resume-work-state openai.yaml exists'
    Assert-True (Test-Path -LiteralPath $scenarioPath -PathType Leaf) 'resume-work-state scenario exists'
    if (-not (Test-Path -LiteralPath $skillPath -PathType Leaf)) {
        return
    }

    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $skillPath
    Assert-True ($content -match '(?m)^name: resume-work-state$') 'resume-work-state has the expected name'
    Assert-True ($content -match '(?m)^description: Use when ') 'resume-work-state description starts with Use when'
    Assert-True ($content -notmatch 'TODO|TBD') 'resume-work-state has no placeholders'
    foreach ($scriptReference in @(
        'scripts/work-state/inspect-git-state.ps1',
        'scripts/work-state/check-state-docs.ps1',
        'scripts/work-state/verify-remote-head.ps1'
    )) {
        Assert-True ($content -match [regex]::Escape($scriptReference)) "resume-work-state references $scriptReference"
    }
    Assert-True ($content -match [regex]::Escape('git pull --ff-only')) 'resume-work-state requires fast-forward-only pull'
    Assert-True ($content -match 'Do not stash') 'resume-work-state forbids automatic stash on a dirty tree'
    Assert-True ($content -match 'Actual Git state, source, and fresh tests override saved prose') 'resume-work-state prioritizes executable evidence'
    Assert-True ($content -match 'Pre-Work Summary Contract') 'resume-work-state defines a pre-work summary'

    if (Test-Path -LiteralPath $metadataPath -PathType Leaf) {
        $metadata = Get-Content -Raw -Encoding UTF8 -LiteralPath $metadataPath
        Assert-True ($metadata -match [regex]::Escape('$resume-work-state')) 'resume-work-state metadata default prompt invokes the skill'
        Assert-True ($metadata -match '(?m)^\s*allow_implicit_invocation: true$') 'resume-work-state allows implicit invocation'
    }

    if (Test-Path -LiteralPath $scenarioPath -PathType Leaf) {
        $scenario = Get-Content -Raw -Encoding UTF8 -LiteralPath $scenarioPath
        Assert-True ($scenario -match '## Prompt') 'resume-work-state scenario has a prompt'
        Assert-True ($scenario -match '## Evaluation contract') 'resume-work-state scenario has an evaluation contract'
    }
}

if ($Suite -in @('All', 'Docs')) {
    Test-Docs
}
if ($Suite -in @('All', 'Scripts')) {
    Test-Scripts
}
if ($Suite -in @('All', 'Scripts', 'PublicSafety')) {
    Test-PublicSafety
}
if ($Suite -in @('All', 'Integration')) {
    Test-Integration
}
if ($Suite -in @('All', 'SaveSkill')) {
    Test-SaveSkill
}
if ($Suite -in @('All', 'ResumeSkill')) {
    Test-ResumeSkill
}

Write-Host "RESULT: $script:Passes passed, $script:Failures failed"
if ($script:Failures -gt 0) {
    exit 1
}

exit 0
