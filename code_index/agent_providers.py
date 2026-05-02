"""Registry of local coding-agent provider presets and capabilities."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CAPABILITY_COMMAND_PRESET = "command_preset"
CAPABILITY_CUSTOM_COMMAND = "custom_command"
CAPABILITY_INLINE_PROVIDER_PROMPT = "inline_provider_prompt"
CAPABILITY_PROVIDER_PROMPT_FILE = "provider_prompt_file"
CAPABILITY_LAST_MESSAGE_FILE = "last_message_file"
CAPABILITY_MCP_CONFIG_FILE = "mcp_config_file"
CAPABILITY_TASK_JSON_FILE = "task_json_file"
CAPABILITY_JSON_OUTPUT = "json_output"
CAPABILITY_STREAM_JSON_OUTPUT = "stream_json_output"
PROVIDER_SPECS_ENV_VAR = "CODE_INDEX_AGENT_PROVIDER_SPECS"


@dataclass(frozen=True)
class AgentProvider:
    id: str
    display_name: str
    command_preset: str | None
    capabilities: frozenset[str]

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


def normalize_provider_id(provider_id: str | None) -> str:
    return (provider_id or "custom").strip().lower() or "custom"


def _default_display_name(provider_id: str) -> str:
    return provider_id.replace("_", " ").replace("-", " ").title()


def _required_text(
    value: Any,
    *,
    field_name: str,
    source: str,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{source}: provider {field_name} must be a non-empty string")
    return value.strip()


def _optional_text(
    value: Any,
    *,
    field_name: str,
    source: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{source}: provider {field_name} must be a string")
    return value.strip() or None


def _capabilities_from_value(value: Any, *, source: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, list):
        raise ValueError(f"{source}: provider capabilities must be a list of strings")
    capabilities: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"{source}: provider capabilities must be a list of strings"
            )
        capability = item.strip()
        if capability not in capabilities:
            capabilities.append(capability)
    return frozenset(capabilities)


@dataclass(frozen=True)
class AgentProviderSpec:
    id: str
    display_name: str | None = None
    command_preset: str | None = None
    capabilities: frozenset[str] = frozenset()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        source: str = "provider spec",
    ) -> AgentProviderSpec:
        if not isinstance(value, Mapping):
            raise ValueError(f"{source}: provider spec must be an object")
        provider_id = normalize_provider_id(
            _required_text(value.get("id"), field_name="id", source=source)
        )
        display_name = _optional_text(
            value.get("display_name"),
            field_name="display_name",
            source=source,
        )
        command_preset = _optional_text(
            value.get("command_preset"),
            field_name="command_preset",
            source=source,
        )
        return cls(
            id=provider_id,
            display_name=display_name,
            command_preset=command_preset,
            capabilities=_capabilities_from_value(
                value.get("capabilities"),
                source=source,
            ),
        )

    def to_provider(self) -> AgentProvider:
        capabilities = set(self.capabilities)
        if self.command_preset:
            capabilities.add(CAPABILITY_COMMAND_PRESET)
        return AgentProvider(
            id=normalize_provider_id(self.id),
            display_name=self.display_name or _default_display_name(self.id),
            command_preset=self.command_preset,
            capabilities=frozenset(capabilities),
        )


@dataclass(frozen=True)
class AgentProviderRegistry:
    providers: dict[str, AgentProvider]
    provider_order: tuple[str, ...]

    @classmethod
    def from_specs(cls, specs: Iterable[AgentProviderSpec]) -> AgentProviderRegistry:
        providers: dict[str, AgentProvider] = {}
        provider_order: list[str] = []
        for spec in specs:
            provider = spec.to_provider()
            if provider.id not in providers:
                provider_order.append(provider.id)
            providers[provider.id] = provider
        return cls(providers=providers, provider_order=tuple(provider_order))

    def command_templates(self) -> dict[str, str]:
        templates: dict[str, str] = {}
        for provider_id, provider in self.providers.items():
            if provider.command_preset:
                templates[provider_id] = provider.command_preset
        return templates


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


def _builtin_provider_specs() -> tuple[AgentProviderSpec, ...]:
    return (
        AgentProviderSpec(
            id="custom",
            display_name="Custom",
            command_preset=None,
            capabilities=frozenset({CAPABILITY_CUSTOM_COMMAND}),
        ),
        AgentProviderSpec(
            id="claude",
            display_name="Claude",
            command_preset=(
                "claude -p --output-format stream-json "
                "--mcp-config {mcp_config_file} < {provider_prompt_file}"
            ),
            capabilities=frozenset(
                {
                    CAPABILITY_COMMAND_PRESET,
                    CAPABILITY_PROVIDER_PROMPT_FILE,
                    CAPABILITY_MCP_CONFIG_FILE,
                    CAPABILITY_STREAM_JSON_OUTPUT,
                }
            ),
        ),
        AgentProviderSpec(
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
        AgentProviderSpec(
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
        AgentProviderSpec(
            id="opencode",
            display_name="OpenCode",
            command_preset=(
                "opencode run --dir {root} --format json "
                "--file {task_json} {provider_prompt}"
            ),
            capabilities=frozenset(
                {
                    CAPABILITY_COMMAND_PRESET,
                    CAPABILITY_INLINE_PROVIDER_PROMPT,
                    CAPABILITY_TASK_JSON_FILE,
                    CAPABILITY_JSON_OUTPUT,
                }
            ),
        ),
    )


def _provider_spec_values(payload: Any, *, source: str) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "providers" not in payload:
            return [payload]
        providers = payload["providers"]
        if not isinstance(providers, list):
            raise ValueError(f"{source}: providers must be a list")
        return providers
    raise ValueError(f"{source}: provider specs must be an object or list")


def load_provider_specs_from_json(
    path: str | os.PathLike[str],
) -> list[AgentProviderSpec]:
    spec_path = Path(path).expanduser()
    try:
        text = spec_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(
            f"{spec_path}: provider spec JSON could not be read: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{spec_path}: provider spec JSON is invalid: {exc}") from exc
    values = _provider_spec_values(payload, source=str(spec_path))
    specs: list[AgentProviderSpec] = []
    for index, value in enumerate(values):
        specs.append(
            AgentProviderSpec.from_mapping(
                value,
                source=f"{spec_path}:providers[{index}]",
            )
        )
    return specs


def _optional_provider_spec_paths() -> list[Path]:
    raw = os.environ.get(PROVIDER_SPECS_ENV_VAR)
    if raw is None or not raw.strip():
        return []
    return [
        Path(value.strip()).expanduser()
        for value in raw.split(os.pathsep)
        if value.strip()
    ]


def _default_registry() -> AgentProviderRegistry:
    specs: list[AgentProviderSpec] = list(_builtin_provider_specs())
    for path in _optional_provider_spec_paths():
        specs.extend(load_provider_specs_from_json(path))
    return AgentProviderRegistry.from_specs(specs)


_REGISTRY = _default_registry()
_PROVIDERS: dict[str, AgentProvider] = _REGISTRY.providers
_PROVIDER_ORDER = _REGISTRY.provider_order

# Mutable for tests and local overrides that patch legacy PROVIDER_COMMANDS.
PROVIDER_COMMANDS: dict[str, str] = _REGISTRY.command_templates()


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
