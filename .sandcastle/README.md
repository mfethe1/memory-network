# Sandcastle Setup for code_index

Cross-platform AI agent sandboxing using [Sandcastle](https://github.com/mattpocock/sandcastle).

## Supported Platforms

| Platform | Runtime | Status | Notes |
|----------|---------|--------|-------|
| Windows + WSL2 | Docker Desktop | Recommended | Best performance when repo is inside WSL filesystem |
| Windows (native) | Docker Desktop | Supported | May have slower I/O on `/mnt/` drives |
| macOS | Docker Desktop | Supported | Intel & Apple Silicon |
| Linux | Docker Engine | Supported | Native performance |
| Linux | Podman | Supported | Rootless alternative |
| Any | Vercel | Cloud | Requires `@vercel/sandbox` setup |

## Supported Agents

| Agent | Auth Method | CLI |
|-------|-------------|-----|
| **Claude Code** | Subscription (`claude login`) or `ANTHROPIC_API_KEY` | `claude` |
| **Codex** | `OPENAI_API_KEY` or `~/.codex/config.toml` | `codex` |
| **Kimi** | Subscription (`kimi login`) or `KIMI_API_KEY` | `kimi` |

Auth directories (`~/.claude`, `~/.kimi`, `~/.codex`) are **auto-mounted** into the sandbox when they exist, so subscription logins work transparently.

## Quick Start

### 1. Install dependencies

```bash
npm install
```

### 2. Configure environment

```bash
cp .sandcastle/.env.example .sandcastle/.env
# Edit .sandcastle/.env - only fill in keys for agents you use
```

If you use **subscription login** instead of API keys, just ensure you're logged in on the host:

```bash
claude login    # Claude Code
kimi login      # Kimi
# Codex uses API keys by default
```

### 3. Build the sandbox image

```bash
# Automatic (first run will build)
npm run sandcastle:run

# Or explicit build
npm run sandcastle:build
```

### 4. Run an agent

```bash
# Using npm scripts (uses auto-detected agent)
npm run sandcastle:plan       # Plan a feature
npm run sandcastle:implement  # Implement with up to 5 iterations
npm run sandcastle:review     # Review current changes
npm run sandcastle:run        # Default explore mode

# Run a specific work package on an explicit branch
.\scripts\sandcastle.ps1 -Mode implement -Agent codex `
  -TaskFile .sandcastle\tasks\openclaw-m2-provider-adapters.md `
  -Branch agent/openclaw-m2-provider-adapters

# Override agent via environment
SANDCASTLE_AGENT=kimi npm run sandcastle:implement
MODEL=kimi-k2-0711-preview npm run sandcastle:plan

# Using helper scripts (auto-detects provider + agent)
# Windows (PowerShell):
.\scripts\sandcastle.ps1 -Mode implement -Agent kimi

# macOS / Linux / WSL (Bash):
./scripts/sandcastle.sh implement auto kimi
```

On Windows worktrees, `scripts/sandcastle.ps1` applies the repo-local
Sandcastle 0.5.7 git-mount patch before launch. Re-run `npm install` freely;
the launcher reapplies the patch when needed.

## Architecture

```
.sandcastle/
|-- tsconfig.json
|-- .gitignore
|-- .env.example
|-- .env
|-- Dockerfile          # Node 22 + Python 3 + Claude Code + Codex + Kimi
|-- prompt.md           # Agent instructions for code_index
|-- README.md           # This file
|-- main.ts             # AFK orchestration (plan/implement/review)
|-- interactive.ts      # Interactive session launcher
`-- lib/
    |-- platform.ts     # OS/provider/agent auto-detection
    `-- kimi-provider.ts # Custom Sandcastle agent provider for Kimi
```

## Platform-Specific Notes

### Windows + WSL2

Docker Desktop with WSL2 backend is the recommended setup.

- **Performance**: Keep the repo inside the WSL filesystem (`~/projects/...`) rather than on a Windows drive (`/mnt/e/...`) for significantly faster bind mounts.
- **Paths**: Sandcastle handles path translation automatically when using Docker Desktop.
- **Line endings**: The Dockerfile configures Git to use LF line endings inside the container.

### macOS

Docker Desktop works out of the box. Apple Silicon users: the base image (`node:22-slim`) supports `arm64` natively.

### Linux

You can use either Docker Engine or Podman:

```bash
# Force Podman
SANDCASTLE_PROVIDER=podman npm run sandcastle:run
```

Podman runs rootless by default, which is more secure.

## Agent-Specific Notes

### Claude Code (Subscription)

No API key needed if you've run `claude login` on the host. The `~/.claude` directory is bind-mounted into the sandbox so the session/auth state is shared.

### Kimi (Subscription)

No API key needed if you've run `kimi login` on the host. The `~/.kimi` directory is bind-mounted into the sandbox.

### Codex

Codex requires an `OPENAI_API_KEY` by default. You can also pre-configure `~/.codex/config.toml` on the host, which is mounted into the sandbox at `/home/agent/.codex/`.

## Customization

### Change the agent

```bash
SANDCASTLE_AGENT=codex npm run sandcastle:implement
```

### Change the model

```bash
MODEL=claude-opus-4-6 npm run sandcastle:implement
MODEL=kimi-k2-0711-preview npm run sandcastle:plan
```

### Change the provider

```bash
SANDCASTLE_PROVIDER=podman npm run sandcastle:run
```

### Add project-specific setup

Edit `.sandcastle/Dockerfile` to install additional tools or language runtimes.

Edit `.sandcastle/prompt.md` to change the agent instructions.

### Branch strategies

The orchestrator uses three strategies:

- `head` - fastest; writes directly to the current worktree (default for explore)
- `merge-to-head` - safer; temp branch merged back when done (default for review)
- `branch` - explicit named branch (default for plan/implement)

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `docker: not found` | Install Docker Desktop or Podman |
| Slow file operations on Windows | Move repo inside WSL filesystem (`~/...`) |
| Image build fails | Check Docker is running: `docker info` |
| Agent can't find Python | The Dockerfile installs `python3`; use `python3` not `python` |
| Permission denied | The container runs as `agent` user; ensure files are readable |
| Claude/Codex/Kimi not authenticated | Run `claude login`, `kimi login`, or set the API key in `.sandcastle/.env` |
| Kimi not detected | Ensure `kimi --version` works on the host PATH |

## References

- [Sandcastle GitHub](https://github.com/mattpocock/sandcastle)
- [Sandcastle API Docs](https://github.com/mattpocock/sandcastle#api)
- [Docker Desktop WSL2](https://docs.docker.com/desktop/wsl/)
- [Podman rootless](https://github.com/containers/podman/blob/main/docs/tutorials/rootless_tutorial.md)
- [Kimi CLI Docs](https://moonshotai.github.io/kimi-cli/)
