[CmdletBinding()]
param(
    [string]$ProjectRoot = '.'
)

$ErrorActionPreference = 'Stop'
$violations = New-Object System.Collections.Generic.List[string]

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    Write-Error "Project root does not exist: $ProjectRoot"
    exit 2
}

$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path

$requirements = @{
    'docs/project/requirements.md' = @('Project Purpose', 'Current Scope', 'Confirmed Requirements', 'Analysis Information Boundary', 'Future Features Outside MVP')
    'docs/project/decisions.md' = @('Recording Rules', 'Accepted Decisions', 'Rejected or Superseded Options')
    'docs/project/plan.md' = @('Current Milestone', 'Completed', 'In Progress', 'Not Started')
    'docs/project/status.md' = @('Current Phase', 'Git State', 'Completed', 'In Progress', 'Verification Results', 'Known Issues', 'Open Questions', 'Next Actions', 'Important Files')
    'docs/project/public-data-policy.md' = @('Public Information', 'Excluded Information', 'Default Local Data Location')
}

$requiredFiles = @('AGENTS.md', 'README.md', '.gitignore', '.gitattributes', '.editorconfig') + @($requirements.Keys)
foreach ($relativePath in $requiredFiles) {
    $path = Join-Path $ProjectRoot $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $violations.Add("Missing required file: $relativePath")
    }
}

foreach ($relativePath in $requirements.Keys) {
    $path = Join-Path $ProjectRoot $relativePath
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        continue
    }
    $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $path
    foreach ($marker in $requirements[$relativePath]) {
        if ($content -notmatch [regex]::Escape($marker)) {
            $violations.Add("Missing marker '$marker' in $relativePath")
        }
    }
}

$agentsPath = Join-Path $ProjectRoot 'AGENTS.md'
if (Test-Path -LiteralPath $agentsPath -PathType Leaf) {
    $agentsLines = @(Get-Content -Encoding UTF8 -LiteralPath $agentsPath)
    $agentsContent = $agentsLines -join "`n"
    if ($agentsLines.Count -gt 60) {
        $violations.Add("AGENTS.md has $($agentsLines.Count) lines; maximum is 60")
    }
    foreach ($skillName in @('$save-work-state', '$resume-work-state')) {
        if ($agentsContent -notmatch [regex]::Escape($skillName)) {
            $violations.Add("AGENTS.md does not route to $skillName")
        }
    }
}

$readmePath = Join-Path $ProjectRoot 'README.md'
if (Test-Path -LiteralPath $readmePath -PathType Leaf) {
    $readme = Get-Content -Raw -Encoding UTF8 -LiteralPath $readmePath
    foreach ($target in @('docs/project/status.md', 'docs/project/requirements.md', 'docs/project/decisions.md', 'docs/project/plan.md')) {
        if ($readme -notmatch [regex]::Escape($target)) {
            $violations.Add("README.md does not link to $target")
        }
    }
}

if ($violations.Count -gt 0) {
    foreach ($violation in $violations) {
        Write-Host "VIOLATION: $violation" -ForegroundColor Red
    }
    Write-Host "State document check failed with $($violations.Count) violation(s)."
    exit 1
}

Write-Host 'State document check passed.'
exit 0
