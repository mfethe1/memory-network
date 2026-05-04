"""Passive OpenClaw context manager primitives for Milestone 1."""

from code_index.openclaw_context.health import ContextHealthInputs
from code_index.openclaw_context.health import evaluate_context_health
from code_index.openclaw_context.handoff import HandoffRequest
from code_index.openclaw_context.handoff import maybe_propose_handoff
from code_index.openclaw_context.manifest import ContextManifestBuilder
from code_index.openclaw_context.manifest import ManifestRequest
from code_index.openclaw_context.models import ContextHealthEvent
from code_index.openclaw_context.models import ContextManifest
from code_index.openclaw_context.models import ContextPointer
from code_index.openclaw_context.models import ContextSource
from code_index.openclaw_context.models import HandoffPacket
from code_index.openclaw_context.models import HostContextMetrics
from code_index.openclaw_context.policy import ContextRetrievalPolicy
from code_index.openclaw_context.policy import detect_quality_gate_flags
from code_index.openclaw_context.policy import hold_assignment_for_avoid_pointers
from code_index.openclaw_context.store import SQLiteContextStore

__all__ = [
    "ContextHealthEvent",
    "ContextHealthInputs",
    "ContextManifest",
    "ContextManifestBuilder",
    "ContextPointer",
    "ContextRetrievalPolicy",
    "ContextSource",
    "HandoffPacket",
    "HandoffRequest",
    "HostContextMetrics",
    "ManifestRequest",
    "SQLiteContextStore",
    "detect_quality_gate_flags",
    "evaluate_context_health",
    "hold_assignment_for_avoid_pointers",
    "maybe_propose_handoff",
]
