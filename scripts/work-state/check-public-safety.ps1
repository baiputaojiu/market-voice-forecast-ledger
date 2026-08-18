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
    '.pytest_cache', '.mypy_cache', '.ruff_cache', '.idea', '.vscode',
    '.worktrees', 'dist', 'build', 'coverage'
)
$allowedCredentialSourceFiles = @(
    'src/market_voice_forecast_ledger/credentials/__init__.py',
    'src/market_voice_forecast_ledger/credentials/windows.py'
)
$forbiddenExtensions = @(
    '.db', '.sqlite', '.sqlite3', '.wav', '.mp3', '.m4a', '.aac',
    '.flac', '.ogg', '.mp4', '.mkv', '.webm', '.mov', '.safetensors',
    '.onnx', '.pt', '.pth', '.pfx', '.p12', '.key', '.pem', '.pyc',
    '.pyo', '.pyd', '.log', '.tmp', '.bak', '.part', '.user', '.suo'
)
$forbiddenFileNames = @('.DS_Store', 'Thumbs.db', 'desktop.ini')
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

function Invoke-GitBytes {
    param([Parameter(Mandatory)][string]$ArgumentLine)

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = 'git'
    $startInfo.Arguments = $ArgumentLine
    $startInfo.WorkingDirectory = $scanRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    $memory = New-Object System.IO.MemoryStream
    try {
        if (-not $process.Start()) {
            throw 'Unable to start Git.'
        }
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $process.StandardOutput.BaseStream.CopyTo($memory)
        $process.WaitForExit()
        $null = $stderrTask.Result
        return [PSCustomObject]@{
            ExitCode = $process.ExitCode
            Bytes = $memory.ToArray()
        }
    }
    catch {
        return [PSCustomObject]@{
            ExitCode = -1
            Bytes = [byte[]]@()
        }
    }
    finally {
        $memory.Dispose()
        $process.Dispose()
    }
}

function ConvertFrom-StrictGitUtf8 {
    param([Parameter(Mandatory)][AllowEmptyCollection()][byte[]]$Bytes)

    $utf8 = [System.Text.UTF8Encoding]::new($false, $true)
    $utf8.GetString($Bytes)
}

function ConvertFrom-NulTerminatedList {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Value)

    if ($Value.Length -eq 0) {
        return @()
    }
    if ($Value[$Value.Length - 1] -ne [char]0) {
        throw 'Git output was not NUL terminated.'
    }
    @($Value.Substring(0, $Value.Length - 1).Split([char[]]@([char]0)))
}

function Get-StagedIndexEntries {
    $result = Invoke-GitBytes -ArgumentLine '-c core.quotePath=false ls-files --stage -z'
    if ($result.ExitCode -ne 0) {
        Write-Host 'Unable to inspect staged index.'
        exit 2
    }

    try {
        $records = ConvertFrom-NulTerminatedList (ConvertFrom-StrictGitUtf8 $result.Bytes)
    }
    catch {
        Write-Host 'Unable to inspect staged index.'
        exit 2
    }

    $entries = @{}
    foreach ($record in $records) {
        $match = [regex]::Match(
            $record,
            '\A(?<mode>[0-9]{6}) (?<oid>[0-9a-fA-F]{40,64}) (?<stage>[0-3])\t(?<path>[\s\S]*)\z'
        )
        if (-not $match.Success) {
            Write-Host 'Unable to inspect staged index.'
            exit 2
        }
        if ($match.Groups['stage'].Value -ne '0') {
            continue
        }
        $entryPath = $match.Groups['path'].Value
        if ($entries.ContainsKey($entryPath)) {
            Write-Host 'Unable to inspect staged index.'
            exit 2
        }
        $entries[$entryPath] = [PSCustomObject]@{
            Mode = $match.Groups['mode'].Value
            ObjectId = $match.Groups['oid'].Value.ToLowerInvariant()
        }
    }
    $entries
}

function Read-StagedBlob {
    param([Parameter(Mandatory)][string]$ObjectId)

    if ($ObjectId -notmatch '\A[0-9a-f]{40,64}\z') {
        return $null
    }
    $result = Invoke-GitBytes -ArgumentLine "cat-file blob $ObjectId"
    if ($result.ExitCode -ne 0) {
        return $null
    }
    ,$result.Bytes
}

function ConvertFrom-FileBytes {
    param([Parameter(Mandatory)][byte[]]$Bytes)

    $offset = 0
    if ($Bytes.Length -ge 4 -and $Bytes[0] -eq 0x00 -and $Bytes[1] -eq 0x00 -and $Bytes[2] -eq 0xFE -and $Bytes[3] -eq 0xFF) {
        $encoding = [System.Text.UTF32Encoding]::new($true, $false, $true)
        $offset = 4
    }
    elseif ($Bytes.Length -ge 4 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xFE -and $Bytes[2] -eq 0x00 -and $Bytes[3] -eq 0x00) {
        $encoding = [System.Text.UTF32Encoding]::new($false, $false, $true)
        $offset = 4
    }
    elseif ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        $encoding = [System.Text.UTF8Encoding]::new($false, $true)
        $offset = 3
    }
    elseif ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFE -and $Bytes[1] -eq 0xFF) {
        $encoding = [System.Text.UnicodeEncoding]::new($true, $false, $true)
        $offset = 2
    }
    elseif ($Bytes.Length -ge 2 -and $Bytes[0] -eq 0xFF -and $Bytes[1] -eq 0xFE) {
        $encoding = [System.Text.UnicodeEncoding]::new($false, $false, $true)
        $offset = 2
    }
    else {
        $encoding = [System.Text.UTF8Encoding]::new($false, $true)
    }
    $encoding.GetString($Bytes, $offset, $Bytes.Length - $offset)
}

function Get-RelativeFiles {
    $isGit = Test-GitRepository
    if ($Mode -eq 'Staged') {
        if (-not $isGit) {
            Write-Error 'Staged mode requires a Git repository.'
            exit 2
        }
        $result = Invoke-GitBytes -ArgumentLine '-c core.quotePath=false diff --cached --name-only --diff-filter=ACMR -z'
        if ($result.ExitCode -ne 0) {
            Write-Host 'Unable to list staged files.'
            exit 2
        }
        try {
            return @(ConvertFrom-NulTerminatedList (ConvertFrom-StrictGitUtf8 $result.Bytes))
        }
        catch {
            Write-Host 'Unable to list staged files.'
            exit 2
        }
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
        $normalized = $relative -replace '\\', '/'
        $segments = @($normalized -split '/')
        $isAllowedCredentialSource = $allowedCredentialSourceFiles -ccontains $normalized
        $excluded = $false
        foreach ($segment in $segments) {
            if ($forbiddenDirectories -contains $segment -and
                -not ($segment -eq 'credentials' -and $isAllowedCredentialSource)) {
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
$stagedIndexEntries = if ($Mode -eq 'Staged') { Get-StagedIndexEntries } else { $null }
foreach ($relativePath in $relativeFiles) {
    $normalized = $relativePath -replace '\\', '/'
    $segments = @($normalized -split '/')
    $isAllowedCredentialSource = $allowedCredentialSourceFiles -ccontains $normalized
    foreach ($segment in $segments) {
        if ($forbiddenDirectories -contains $segment -and
            -not ($segment -eq 'credentials' -and $isAllowedCredentialSource)) {
            $violations.Add("Forbidden directory in path: $normalized")
            break
        }
    }

    $fileName = Split-Path -Leaf $normalized
    $extension = [System.IO.Path]::GetExtension($fileName).ToLowerInvariant()
    $isCoverageFile = $fileName -eq '.coverage' -or $fileName -like '.coverage.*'
    $isDatabaseSidecar = $fileName -like '*.db-*' -or
        $fileName -like '*.sqlite-*' -or $fileName -like '*.sqlite3-*'
    if ($forbiddenExtensions -contains $extension -or
        $forbiddenFileNames -contains $fileName -or
        $isCoverageFile -or $isDatabaseSidecar -or
        $fileName -eq '.env' -or $fileName -like '.env.*') {
        if ($fileName -ne '.env.example') {
            $violations.Add("Forbidden file type: $normalized")
        }
    }

    if ($Mode -eq 'Staged') {
        if (-not $stagedIndexEntries.ContainsKey($relativePath)) {
            $violations.Add("Unable to inspect staged file content: $normalized")
            continue
        }
        $bytes = Read-StagedBlob -ObjectId $stagedIndexEntries[$relativePath].ObjectId
        if ($null -eq $bytes) {
            $violations.Add("Unable to inspect staged file content: $normalized")
            continue
        }
        if ($bytes.LongLength -gt $MaxFileBytes) {
            $violations.Add("File exceeds $MaxFileBytes bytes: $normalized ($($bytes.LongLength) bytes)")
            continue
        }
        if ($binaryExtensions -contains $extension) {
            continue
        }
        try {
            $content = ConvertFrom-FileBytes $bytes
        }
        catch {
            $violations.Add("Unable to inspect staged file content: $normalized")
            continue
        }
        if ($content.IndexOf([char]0) -ge 0) {
            $violations.Add("Unable to inspect staged file content: $normalized")
            continue
        }
        foreach ($pattern in $secretPatterns) {
            if ($content -match $pattern) {
                $violations.Add("Possible secret detected: $normalized")
                break
            }
        }
        continue
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
        $violations.Add("Unable to inspect file content: $normalized")
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
