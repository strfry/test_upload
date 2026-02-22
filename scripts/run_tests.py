#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run project test suite via pytest.")
    parser.add_argument("-q", "--quiet", action="store_true", help="Run pytest in quiet mode")
    parser.add_argument("-v", "--verbose", action="store_true", help="Run pytest in verbose mode")
    args = parser.parse_args()

    try:
        import pytest
    except ModuleNotFoundError:
        print("pytest is not installed. Install dev deps: python3 -m pip install -r requirements-dev.txt")
        return 2

    pytest_args: list[str] = []
    if args.quiet:
        pytest_args.append("-q")
    elif args.verbose:
        pytest_args.append("-v")
    pytest_args.append("tests")
    return int(pytest.main(pytest_args))


if __name__ == "__main__":
    sys.exit(main())
