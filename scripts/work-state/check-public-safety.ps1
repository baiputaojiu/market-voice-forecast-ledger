[CmdletBinding()]
param(
    [string]$Path = '.',
    [ValidateSet('WorkingTree', 'Staged')]
    [string]$Mode = 'WorkingTree',
    [long]$MaxFileBytes = 10485760
)

$ErrorActionPreference = 'Stop'
$violations = New-Object System.Collections.Generic.List[string]

if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
    Write-Error "Scan path does not exist: $Path"
    exit 2
}

$scanRoot = (Resolve-Path -LiteralPath $Path).Path
$forbiddenDirectories = @(
    '.git', '.stitch', '.cache', '.huggingface', '.whisper',
    'secrets', 'credentials',
    'data', 'storage', 'runtime', 'transcripts', 'audio', 'video',
    'speaker-embeddings', 'analysis-snapshots', 'models', 'logs',
    'tmp', 'temp', 'node_modules', '.venv', 'venv', '__pycache__',
    'dist', 'build', 'coverage'
)
$forbiddenExtensions = @(
    '.db', '.sqlite', '.sqlite3', '.wav', '.mp3', '.m4a', '.aac',
    '.flac', '.ogg', '.mp4', '.mkv', '.webm', '.mov', '.safetensors',
    '.onnx', '.pt', '.pth', '.pfx', '.p12', '.key'
)
$binaryExtensions = @('.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.woff', '.woff2')
$secretPatterns = @(
    '-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    '(?im)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|authorization|cookie)\s*[:=]\s*["'']?(?!(?:example|dummy|placeholder|changeme|your[-_]?|test[-_]?|not[-_]?a[-_]?real))([A-Za-z0-9_./+=:-]{12,})',
    '(?i)\bgh[pousr]_[A-Za-z0-9]{20,}\b',
    '(?i)\bsk-[A-Za-z0-9_-]{20,}\b',
    '(?i)\bAIza[0-9A-Za-z_-]{30,}\b',
    '(?im)\bBearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}'
)

function Test-GitRepository {
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $null = & git -C $scanRoot rev-parse --show-toplevel 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorAction
    $exitCode -eq 0
}

function Get-RelativeFiles {
    $isGit = Test-GitRepository
    if ($Mode -eq 'Staged') {
        if (-not $isGit) {
            Write-Error 'Staged mode requires a Git repository.'
            exit 2
        }
        $output = & git -C $scanRoot diff --cached --name-only --diff-filter=ACMR 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'Unable to list staged files.'
            exit 2
        }
        return @($output | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    if ($isGit) {
        $output = & git -C $scanRoot ls-files --cached --others --exclude-standard 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Error 'Unable to list Git working-tree files.'
            exit 2
        }
        return @($output | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    }

    $files = Get-ChildItem -LiteralPath $scanRoot -Recurse -Force -File
    $relativeFiles = New-Object System.Collections.Generic.List[string]
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($scanRoot.Length).TrimStart('\', '/')
        $segments = @($relative -split '[\\/]')
        $excluded = $false
        foreach ($segment in $segments) {
            if ($forbiddenDirectories -contains $segment) {
                $excluded = $true
                break
            }
        }
        if (-not $excluded) {
            $relativeFiles.Add($relative)
        }
    }
    return @($relativeFiles)
}

$relativeFiles = @(Get-RelativeFiles)
foreach ($relativePath in $relativeFiles) {
    $normalized = $relativePath -replace '\\', '/'
    $segments = @($normalized -split '/')
    foreach ($segment in $segments) {
        if ($forbiddenDirectories -contains $segment) {
            $violations.Add("Forbidden directory in path: $normalized")
            break
        }
    }

    $fileName = Split-Path -Leaf $normalized
    $extension = [System.IO.Path]::GetExtension($fileName).ToLowerInvariant()
    if ($forbiddenExtensions -contains $extension -or $fileName -eq '.env' -or $fileName.StartsWith('.env.')) {
        if ($fileName -ne '.env.example') {
            $violations.Add("Forbidden file type: $normalized")
        }
    }

    $fullPath = Join-Path $scanRoot ($relativePath -replace '/', [System.IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
        continue
    }
    $item = Get-Item -LiteralPath $fullPath
    if ($item.Length -gt $MaxFileBytes) {
        $violations.Add("File exceeds $MaxFileBytes bytes: $normalized ($($item.Length) bytes)")
        continue
    }
    if ($binaryExtensions -contains $extension) {
        continue
    }

    try {
        $content = [System.IO.File]::ReadAllText($fullPath)
    }
    catch {
        $violations.Add("Unable to inspect file content: $normalized")
        continue
    }
    if ($content.IndexOf([char]0) -ge 0) {
        continue
    }
    foreach ($pattern in $secretPatterns) {
        if ($content -match $pattern) {
            $violations.Add("Possible secret detected: $normalized")
            break
        }
    }
}

if ($violations.Count -gt 0) {
    foreach ($violation in @($violations | Sort-Object -Unique)) {
        Write-Host "VIOLATION: $violation" -ForegroundColor Red
    }
    Write-Host 'Public safety check failed.'
    exit 1
}

Write-Host "Public safety check passed for $($relativeFiles.Count) file(s)."
exit 0
