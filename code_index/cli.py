"""code_index CLI entrypoint.

Subcommand parser construction lives in `code_index.cli_parser`; this module
keeps the public `main()` entrypoint small and stable.
"""

from __future__ import annotations

import sys

from code_index.cli_parser import build_parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
