# Sandcastle sandbox for code_index (Python + Node.js + multi-agent)
# Works on: Windows (WSL2/Docker Desktop), macOS (Docker Desktop), Linux (Docker/Podman)

FROM node:22-slim

# Install system dependencies.
RUN apt-get update && apt-get install -y \
    git \
    curl \
    jq \
    python3 \
    python3-pip \
    python3-venv \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | \
    dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg && \
    chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | \
    tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y gh && \
    rm -rf /var/lib/apt/lists/*

# Install AI agent CLIs globally.
RUN npm install -g @anthropic-ai/claude-code
RUN npm install -g @openai/codex

# Kimi CLI currently requires Python 3.12+. The node:22-slim image ships
# Python 3.11, so install Kimi with uv-managed Python.
RUN python3 -m pip install --break-system-packages --upgrade pip setuptools wheel uv

# Sandcastle's Docker provider runs as UID/GID 1000 on Windows, which is the
# built-in node user in node:22-slim. Use that user with Sandcastle's expected
# home path so git config and CLI auth files are writable.
RUN mkdir -p /home/agent/.kimi/logs /home/agent/.kimi-host /home/agent/.claude /home/agent/.codex/sessions /home/agent/.config && \
    chown -R node:node /home/agent

WORKDIR /workspace
USER node
ENV HOME="/home/agent"
ENV PATH="/home/agent/.local/bin:${PATH}"
RUN uv tool install --python 3.12 kimi-cli

# Ensure git uses LF line endings (cross-platform safety)
RUN git config --global core.autocrlf false && \
    git config --global core.eol lf

# Keep detached Sandcastle containers alive for docker exec.
CMD ["sleep", "infinity"]
