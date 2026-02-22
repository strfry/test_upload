#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prompt_cli import main as prompt_main


def main() -> None:
    prompt_main(["--history"] + sys.argv[1:])


if __name__ == "__main__":
    main()
