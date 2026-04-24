"""Picks the best available parser for a given file."""

from __future__ import annotations

from dataclasses import dataclass

from code_index.parsers.base import Parser
from code_index.parsers.heuristic import HeuristicParser
from code_index.parsers.python_ast import PythonAstParser


@dataclass
class Registry:
    parsers: list[Parser]

    def select(self, rel_path: str) -> Parser:
        for parser in self.parsers:
            if parser.supports(rel_path):
                return parser
        return self.parsers[-1]


def default_registry() -> Registry:
    return Registry(parsers=[PythonAstParser(), HeuristicParser()])
