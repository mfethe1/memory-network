"""Natural-language question → intent classification.

Pure function. No database access. No LLM. Regex over normalized question
text. Each Intent maps to one primary tool plus optional supporting tools.
Every intent carries a `target` slot extracted from the question when the
pattern names one.

When a question matches no pattern, `classify()` returns `Intent.unknown`
with a suggestion to use `search_query` or `search_similar` directly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class IntentKind(str, Enum):
    WHERE = "where"  # "where is X defined"
    CALLERS = "callers"  # "who calls X" / "what uses X"
    IMPACT = "impact"  # "what breaks if I change X"
    TESTS = "tests"  # "which tests cover X"
    REFERENCES = "references"  # "show me call sites of X"
    SIMILAR = "similar"  # "find code like X"
    STRUCTURAL = "structural"  # "find all classes" / "find all imports"
    LITERAL = "literal"  # "search for TODO"
    RANKED = "ranked"  # "find code about jwt expiry"
    OVERVIEW = "overview"  # "give me a tour of this repo"
    HEALTH = "health"  # "is the index healthy"
    UNKNOWN = "unknown"


@dataclass
class Intent:
    kind: IntentKind
    confidence: float  # 0.0–1.0; conservative
    target: str | None = None  # extracted symbol/term
    language: str | None = None
    limit: int = 10
    rationale: str = ""  # why we classified this way
    matched_pattern: str | None = None
    unknown_reason: str | None = None
    supporting_intents: list["Intent"] = field(default_factory=list)

    @classmethod
    def unknown(cls, question: str, reason: str) -> "Intent":
        return cls(
            kind=IntentKind.UNKNOWN,
            confidence=0.0,
            rationale=f"no pattern matched: {question!r}",
            unknown_reason=reason,
        )


# Ordered: more specific patterns first. Each rule has (kind, regex,
# rationale, target-group-name).
# Patterns focus on BARE-ESSENTIAL phrasing; they're conservative on purpose.
_PATTERNS: list[tuple[IntentKind, re.Pattern[str], str]] = [
    # References / call sites
    (
        IntentKind.REFERENCES,
        re.compile(
            r"(?:call\s*sites?|references?|usages?)\s+(?:of\s+|for\s+)?(?P<target>[\w\.]+)",
            re.I,
        ),
        "explicit call-site / reference phrasing",
    ),
    # Impact (broader blast radius than callers)
    (
        IntentKind.IMPACT,
        re.compile(
            r"(?:what\s+(?:would\s+)?breaks?(?:\s+if\s+i\s+(?:change|modify|edit|rename|delete))?|blast\s*radius(?:\s+of)?|impact\s*of|safe\s+to\s+(?:change|delete|rename))\s+[`'\"]?(?P<target>[\w\.]+)[`'\"]?",
            re.I,
        ),
        "impact / blast-radius phrasing",
    ),
    (
        IntentKind.IMPACT,
        re.compile(
            r"(?:what\s+(?:depends\s+on|relies\s+on))\s+(?P<target>[\w\.]+)", re.I
        ),
        "dependency phrasing",
    ),
    # Callers (narrower than impact; 1-hop)
    (
        IntentKind.CALLERS,
        re.compile(
            r"(?:who\s+calls|what\s+calls|callers?\s+of|who\s+uses|what\s+uses)\s+(?P<target>[\w\.]+)",
            re.I,
        ),
        "callers / who-uses phrasing",
    ),
    # Tests
    (
        IntentKind.TESTS,
        re.compile(
            r"(?:which\s+tests?\s+(?:cover|exercise|touch|test)|tests?\s+(?:for|of|covering)|affected\s+tests?\s+(?:for|of)?)\s+(?P<target>[\w\.]+)",
            re.I,
        ),
        "test-coverage phrasing",
    ),
    (
        IntentKind.TESTS,
        re.compile(
            r"what\s+happens\s+to\s+the\s+tests?\s+(?:if\s+i\s+change\s+)?(?P<target>[\w\.]+)",
            re.I,
        ),
        "test-impact phrasing",
    ),
    # Where
    (
        IntentKind.WHERE,
        re.compile(
            r"(?:where\s+is|where.s\s+|where\s+does)\s+[`'\"]?(?P<target>[\w\.]+)[`'\"]?(?:\s+defined|\s+live|\s+come\s+from)?",
            re.I,
        ),
        "where-is phrasing",
    ),
    (
        IntentKind.WHERE,
        re.compile(
            r"(?:definition\s+of|find\s+(?:the\s+)?definition\s+of)\s+(?P<target>[\w\.]+)",
            re.I,
        ),
        "definition-of phrasing",
    ),
    # Similar / semantic
    (
        IntentKind.SIMILAR,
        re.compile(
            r"(?:find\s+code\s+(?:like|similar\s+to)|similar\s+to|like\s+this|code\s+that\s+(?:does|handles))\s+(?P<target>.+?)[\?\.]?$",
            re.I,
        ),
        "semantic-similarity phrasing",
    ),
    # Structural
    (
        IntentKind.STRUCTURAL,
        re.compile(
            r"find\s+all\s+(?P<target>classes|functions|methods|imports|calls|decorators)",
            re.I,
        ),
        "structural `find all` phrasing",
    ),
    # Literal search
    (
        IntentKind.LITERAL,
        re.compile(
            r"^(?:grep\s+(?:for\s+)?|search\s+for\s+the\s+string\s+)(?P<target>[^\?]+?)[\?\.]?$",
            re.I,
        ),
        "explicit literal / grep phrasing",
    ),
    # Health / overview
    (
        IntentKind.HEALTH,
        re.compile(r"(?:is\s+the\s+index\s+healthy|index\s+health|doctor|drift)", re.I),
        "health / doctor phrasing",
    ),
    (
        IntentKind.OVERVIEW,
        re.compile(
            r"(?:give\s+me\s+(?:a\s+)?(?:tour|overview|map|repo\s+map|summary)|^repo[- ]map$|orient(?:ation)?\s+me|high[- ]level|what.?s\s+in\s+this\s+repo)",
            re.I,
        ),
        "repo-overview phrasing",
    ),
    # Ranked / "find code about X"
    (
        IntentKind.RANKED,
        re.compile(
            r"(?:find\s+code\s+about|code\s+for|where\s+do\s+we\s+)(?P<target>.+?)[\?\.]?$",
            re.I,
        ),
        "ranked-retrieval phrasing",
    ),
]


_LANG_HINT = re.compile(
    r"\b(?:in|for)\s+(python|typescript|javascript|rust|go)\b", re.I
)
_LIMIT_HINT = re.compile(r"\btop\s+(\d{1,3})\b", re.I)


def classify(question: str) -> Intent:
    q = question.strip()
    if not q:
        return Intent.unknown(question, "empty question")

    lang = None
    m = _LANG_HINT.search(q)
    if m:
        lang = m.group(1).lower()

    limit = 10
    m = _LIMIT_HINT.search(q)
    if m:
        try:
            limit = max(1, min(100, int(m.group(1))))
        except ValueError:
            pass

    for kind, rx, rationale in _PATTERNS:
        m = rx.search(q)
        if not m:
            continue
        gd = m.groupdict()
        target = (gd.get("target") or "").strip() or None
        if target:
            # Strip surrounding quotes, pronouns, trailing punctuation.
            target = target.strip(".?!,:;\"'` ")
        return Intent(
            kind=kind,
            confidence=0.85 if target else 0.6,
            target=target,
            language=lang,
            limit=limit,
            rationale=rationale,
            matched_pattern=rx.pattern,
        )

    # No classifier match. Fall back to a useful suggestion instead of refusing.
    return Intent(
        kind=IntentKind.UNKNOWN,
        confidence=0.0,
        target=None,
        language=lang,
        limit=limit,
        rationale="no pattern matched",
        unknown_reason="question shape not recognized; try `similar` or `query`",
    )
