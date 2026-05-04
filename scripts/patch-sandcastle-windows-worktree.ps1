#requires -Version 5.1
<#
.SYNOPSIS
    Applies the local Sandcastle Windows worktree mount patch.
.DESCRIPTION
    @ai-hero/sandcastle 0.5.7 resolves git mounts from the host repo path in
    branch mode. When the host repo is itself a Git worktree on Windows, Docker
    receives a Windows .git mount as the container path and fails with
    "invalid mode". The branch worktree's .git file is the correct mount source.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$target = Join-Path $root "node_modules\@ai-hero\sandcastle\dist\SandboxFactory.js"

if (-not (Test-Path $target)) {
    throw "Sandcastle SandboxFactory.js not found. Run npm install first."
}

$text = Get-Content -Raw $target
$original = $text

$headWrong = @'
const gitPath = join(worktreeInfo.path, ".git");
                    return (hooks?.host?.onWorktreeReady
'@
$headRight = @'
const gitPath = join(hostRepoDir, ".git");
                    return (hooks?.host?.onWorktreeReady
'@
$branchWrong = @'
const gitPath = join(hostRepoDir, ".git");
                    return resolveGitMounts(gitPath).pipe
'@
$branchRight = @'
const gitPath = join(worktreeInfo.path, ".git");
                    return resolveGitMounts(gitPath).pipe
'@

$text = $text.Replace($headWrong, $headRight)
$text = $text.Replace($branchWrong, $branchRight)

if (-not $text.Contains($headRight) -or -not $text.Contains($branchRight)) {
    throw "Sandcastle patch target did not match the expected 0.5.7 source."
}

if ($text -eq $original) {
    Write-Host "Sandcastle Windows worktree patch already applied."
    exit 0
}

$text | Set-Content -NoNewline -Encoding UTF8 $target
Write-Host "Applied Sandcastle Windows worktree patch."
