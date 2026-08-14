[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ScenarioPath,
    [ValidateSet('Baseline', 'WithSkill')]
    [string]$Mode = 'Baseline',
    [string]$SkillPath,
    [ValidateRange(1, 10)]
    [int]$Repetitions = 5,
    [switch]$KeepArtifacts
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ScenarioPath -PathType Leaf)) {
    throw "Scenario does not exist: $ScenarioPath"
}
if ($Mode -eq 'WithSkill' -and -not (Test-Path -LiteralPath $SkillPath -PathType Container)) {
    throw "Skill does not exist: $SkillPath"
}

$scenario = Get-Content -Raw -Encoding UTF8 -LiteralPath $ScenarioPath
$promptParts = $scenario -split '## Evaluation contract', 2
$prompt = ($promptParts[0] -replace '(?s)^.*?## Prompt\s*', '').Trim()
if ([string]::IsNullOrWhiteSpace($prompt)) {
    throw 'Scenario prompt is empty.'
}

$prefix = if ($Mode -eq 'WithSkill') { 'mvfl-skill-with-' } else { 'mvfl-skill-baseline-' }
$evalRoot = Join-Path ([System.IO.Path]::GetTempPath()) ($prefix + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $evalRoot | Out-Null

try {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & git init $evalRoot 2>&1 | Out-Null
    $gitExitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    if ($gitExitCode -ne 0) {
        throw 'Unable to create temporary Git repository.'
    }

    if ($Mode -eq 'WithSkill') {
        $skillName = Split-Path -Leaf $SkillPath
        $skillParent = Join-Path $evalRoot '.agents\skills'
        New-Item -ItemType Directory -Path $skillParent -Force | Out-Null
        Copy-Item -LiteralPath $SkillPath -Destination (Join-Path $skillParent $skillName) -Recurse
        $prompt = ('Use $' + $skillName + ".`n`n" + $prompt)
    }

    for ($index = 1; $index -le $Repetitions; $index++) {
        $outputPath = Join-Path $evalRoot ("response-$index.txt")
        $arguments = @(
            'exec', '-',
            '-C', $evalRoot,
            '-m', 'gpt-5.6-sol',
            '-c', 'model_reasoning_effort="max"',
            '-s', 'read-only',
            '--ephemeral',
            '--ignore-user-config',
            '--color', 'never',
            '-o', $outputPath
        )

        $previousErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $prompt | & codex @arguments 2>&1 | Out-Null
        $codexExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousErrorAction
        if ($codexExitCode -ne 0) {
            throw "Codex evaluation repetition $index failed with exit code $codexExitCode."
        }

        Write-Output "=== REP $index ==="
        Get-Content -Raw -Encoding UTF8 -LiteralPath $outputPath
    }

    if ($KeepArtifacts) {
        Write-Output "ARTIFACTS: $evalRoot"
    }
}
finally {
    if (-not $KeepArtifacts -and (Test-Path -LiteralPath $evalRoot)) {
        $fullPath = [System.IO.Path]::GetFullPath($evalRoot)
        $tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
        $leaf = Split-Path -Leaf $fullPath
        $allowedPrefix = $leaf.StartsWith('mvfl-skill-baseline-') -or $leaf.StartsWith('mvfl-skill-with-')
        if (-not $fullPath.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase) -or -not $allowedPrefix) {
            throw "Refusing to remove unexpected evaluation path: $fullPath"
        }
        Remove-Item -LiteralPath $fullPath -Recurse -Force
    }
}
