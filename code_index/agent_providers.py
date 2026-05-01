"""Registry of local coding-agent provider presets and capabilities."""

from __future__ import annotations

import os
from dataclasses import dataclass


CAPABILITY_COMMAND_PRESET = "command_preset"
CAPABILITY_CUSTOM_COMMAND = "custom_command"
CAPABILITY_INLINE_PROVIDER_PROMPT = "inline_provider_prompt"
CAPABILITY_PROVIDER_PROMPT_FILE = "provider_prompt_file"
CAPABILITY_LAST_MESSAGE_FILE = "last_message_file"
CAPABILITY_MCP_CONFIG_FILE = "mcp_config_file"
CAPABILITY_JSON_OUTPUT = "json_output"
CAPABILITY_STREAM_JSON_OUTPUT = "stream_json_output"


@dataclass(frozen=True)
class AgentProvider:
    id: str
    display_name: str
    command_preset: str | None
    capabilities: frozenset[str]

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


_PROVIDER_ORDER = ("custom", "claude", "codex", "kimi")


def _bounded_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))


def _kimi_command_preset() -> str:
    max_ralph_iterations = _bounded_int_env(
        "CODE_INDEX_KIMI_MAX_RALPH_ITERATIONS",
        -1,
        minimum=-1,
        maximum=100_000,
    )
    max_steps_per_turn = _bounded_int_env(
        "CODE_INDEX_KIMI_MAX_STEPS_PER_TURN",
        200,
        minimum=1,
        maximum=10_000,
    )
    return (
        "kimi --work-dir {root} --mcp-config-file {mcp_config_file} "
        "--print --output-format stream-json --thinking "
        f"--max-ralph-iterations {max_ralph_iterations} "
        f"--max-steps-per-turn {max_steps_per_turn} "
        "< {provider_prompt_file}"
    )

_PROVIDERS: dict[str, AgentProvider] = {
    "custom": AgentProvider(
        id="custom",
        display_name="Custom",
        command_preset=None,
        capabilities=frozenset({CAPABILITY_CUSTOM_COMMAND}),
    ),
    "claude": AgentProvider(
        id="claude",
        display_name="Claude",
        command_preset="claude -p {provider_prompt}",
        capabilities=frozenset(
            {
                CAPABILITY_COMMAND_PRESET,
                CAPABILITY_INLINE_PROVIDER_PROMPT,
            }
        ),
    ),
    "codex": AgentProvider(
        id="codex",
        display_name="Codex",
        command_preset=(
            "codex exec -C {root} -s workspace-write --json "
            "-o {last_message} - < {provider_prompt_file}"
        ),
        capabilities=frozenset(
            {
                CAPABILITY_COMMAND_PRESET,
                CAPABILITY_PROVIDER_PROMPT_FILE,
                CAPABILITY_LAST_MESSAGE_FILE,
                CAPABILITY_JSON_OUTPUT,
            }
        ),
    ),
    "kimi": AgentProvider(
        id="kimi",
        display_name="Kimi",
        command_preset=_kimi_command_preset(),
        capabilities=frozenset(
            {
                CAPABILITY_COMMAND_PRESET,
                CAPABILITY_PROVIDER_PROMPT_FILE,
                CAPABILITY_MCP_CONFIG_FILE,
                CAPABILITY_STREAM_JSON_OUTPUT,
            }
        ),
    ),
}

# Mutable for tests and local overrides that patch legacy PROVIDER_COMMANDS.
PROVIDER_COMMANDS: dict[str, str] = {
    provider_id: provider.command_preset
    for provider_id, provider in _PROVIDERS.items()
    if provider.command_preset
}


def normalize_provider_id(provider_id: str | None) -> str:
    return (provider_id or "custom").strip().lower() or "custom"


def provider_choices(*, include_custom: bool = True) -> list[str]:
    choices = list(_PROVIDER_ORDER)
    if not include_custom:
        choices = [provider_id for provider_id in choices if provider_id != "custom"]
    return choices


def is_known_provider(provider_id: str | None) -> bool:
    return normalize_provider_id(provider_id) in _PROVIDERS


def get_provider(provider_id: str | None) -> AgentProvider | None:
    return _PROVIDERS.get(normalize_provider_id(provider_id))


def require_provider(provider_id: str | None) -> AgentProvider:
    normalized = normalize_provider_id(provider_id)
    provider = _PROVIDERS.get(normalized)
    if provider is None:
        raise ValueError(f"unknown agent provider: {normalized}")
    return provider


def provider_display_name(provider_id: str | None, *, default: str | None = None) -> str:
    provider = get_provider(provider_id)
    if provider is not None:
        return provider.display_name
    if default is not None:
        return default
    value = (provider_id or "").strip()
    return value.title() if value else "Agent"


def provider_command_template(provider_id: str | None) -> str | None:
    return PROVIDER_COMMANDS.get(normalize_provider_id(provider_id))


def provider_has_capability(provider_id: str | None, capability: str) -> bool:
    provider = get_provider(provider_id)
    return bool(provider and provider.has_capability(capability))


def provider_registry_payload() -> list[dict[str, object]]:
    return [
        {
            "id": provider.id,
            "display_name": provider.display_name,
            "command_preset": provider_command_template(provider.id),
            "capabilities": sorted(provider.capabilities),
        }
        for provider in (_PROVIDERS[provider_id] for provider_id in _PROVIDER_ORDER)
    ]
