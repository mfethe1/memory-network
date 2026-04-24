"""Natural-language query synthesis.

Translates everyday questions agents ask ("who calls X?", "find code like Y")
into calls against our existing primitives (symbol, impact, tests, query,
grep, similar, repo-map) and returns a structured bundle plus a narrative
summary the consuming LLM can use directly.

Deliberately deterministic: no LLM in the loop. Pattern-matching is a
fixed classifier. The goal is not to *understand* the question — the
consuming agent can already do that. The goal is to turn "who calls
FastAPI?" into `impact FastAPI` without the agent having to figure out
which primitive to invoke.
"""

from code_index.nl.classify import Intent, classify
from code_index.nl.synthesize import answer

__all__ = ["Intent", "classify", "answer"]
