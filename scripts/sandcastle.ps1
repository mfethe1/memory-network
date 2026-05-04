#requires -Version 5.1
<#
.SYNOPSIS
    Cross-platform Sandcastle launcher for Windows (PowerShell).
.DESCRIPTION
    Detects Docker Desktop / WSL and launches Sandcastle with the correct provider.
    Falls back to Podman if available.
    Supports Claude, Codex, and Kimi agents.

.PARAMETER Mode
    Run mode: default, plan, implement, review, interactive

.PARAMETER Provider
    Force a sandbox provider: docker, podman, vercel

.PARAMETER Agent
    Force an agent: claude, codex, kimi

.PARAMETER Model
    Override the default model

.PARAMETER TaskFile
    Markdown file containing the task prompt to pass as TASK_DESCRIPTION.

.PARAMETER Branch
    Explicit branch name for branch-based Sandcastle modes.

.EXAMPLE
    .\scripts\sandcastle.ps1 -Mode plan
    .\scripts\sandcastle.ps1 -Mode implement -Agent kimi -Model kimi-k2-0711-preview
#>
[CmdletBinding()]
param(
    [ValidateSet("default", "plan", "implement", "review", "interactive")]
    [string]$Mode = "default",

    [ValidateSet("docker", "podman", "vercel", "auto")]
    [string]$Provider = "auto",

    [ValidateSet("claude", "codex", "kimi", "auto")]
    [string]$Agent = "auto",

    [string]$Model = "",

    [string]$TaskFile = "",

    [string]$Branch = "",

    [int]$MaxIterations = 0
)

$ErrorActionPreference = "Stop"

# Detect container runtime.
function Test-Command($cmd) {
    try {
        $null = Get-Command $cmd -ErrorAction Stop
        return $true
    } catch {
        return $false
    }
}

$selectedProvider = $Provider
if ($Provider -eq "auto") {
    if (Test-Command "docker") {
        $selectedProvider = "docker"
    } elseif (Test-Command "podman") {
        $selectedProvider = "podman"
    } else {
        Write-Host "No container runtime found. Install Docker Desktop or Podman." -ForegroundColor Red
        exit 1
    }
}

$selectedAgent = $Agent
if ($Agent -eq "auto") {
    if (Test-Command "claude") {
        $selectedAgent = "claude"
    } elseif (Test-Command "kimi") {
        $selectedAgent = "kimi"
    } elseif (Test-Command "codex") {
        $selectedAgent = "codex"
    } else {
        Write-Host "No agent CLI found. Install Claude Code, Kimi, or Codex." -ForegroundColor Red
        exit 1
    }
}

# WSL guidance.
$isWsl = $false
try {
    $release = Get-Content "/proc/sys/kernel/osrelease" -ErrorAction Stop
    $isWsl = $release -match "microsoft|wsl"
} catch {
    # Not WSL
}

if ($isWsl) {
    Write-Host "WSL detected. For best performance, ensure this repo is inside the WSL filesystem (~/...) rather than /mnt/" -ForegroundColor Cyan
}

# Build Docker image if needed.
if ($selectedProvider -eq "docker") {
    $imageName = "sandcastle:code-index"
    $existing = docker images -q $imageName 2>$null
    if (-not $existing) {
        Write-Host "Building Sandcastle image ($imageName)..." -ForegroundColor Yellow
        docker build -t $imageName -f .sandcastle/Dockerfile .
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Docker build failed." -ForegroundColor Red
            exit 1
        }
    }
}

# Run.
$env:SANDCASTLE_PROVIDER = $selectedProvider
$env:SANDCASTLE_AGENT = $selectedAgent
if ($Model) {
    $env:MODEL = $Model
}

Push-Location $PSScriptRoot\..

try {
    & (Join-Path $PSScriptRoot "patch-sandcastle-windows-worktree.ps1") | Out-Host

    $extraArgs = @()
    if ($TaskFile) {
        $extraArgs += @("--task-file", $TaskFile)
    }
    if ($Branch) {
        $extraArgs += @("--branch", $Branch)
    }
    if ($MaxIterations -gt 0) {
        $extraArgs += @("--max-iterations", [string]$MaxIterations)
    }

    switch ($Mode) {
        "interactive" {
            npx tsx .sandcastle/interactive.ts --agent $selectedAgent
        }
        default {
            $flag = "--$Mode"
            if ($Mode -eq "default") { $flag = "" }
            if ($flag) {
                npx tsx .sandcastle/main.ts $flag --agent $selectedAgent @extraArgs
            } else {
                npx tsx .sandcastle/main.ts --agent $selectedAgent @extraArgs
            }
        }
    }
} finally {
    Pop-Location
}
