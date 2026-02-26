#!/usr/bin/env python3
"""Test vision model image processing from the CLI.

Usage:
  PYTHONPATH=. python scripts/test_vision_cli.py /path/to/image.jpg
  PYTHONPATH=. python scripts/test_vision_cli.py /path/to/image.png
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scambaiter.config import load_config
from scambaiter.model_client import ModelClient


async def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    image_path = Path(sys.argv[1])
    if not image_path.exists():
        print(f"Error: {image_path} not found.", file=sys.stderr)
        sys.exit(1)

    config = load_config()
    if not config.hf_vision_model:
        print("Error: HF_VISION_MODEL not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Image: {image_path}")
    print(f"Model: {config.hf_vision_model}")
    print("-" * 60)

    # Read and encode image
    image_data = image_path.read_bytes()
    image_b64 = base64.b64encode(image_data).decode("utf-8")

    # Determine MIME type
    suffix = image_path.suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime_type = mime_map.get(suffix, "image/jpeg")

    # Call vision model
    client = ModelClient(config=config)
    try:
        description = await client.call_hf_vision(image_b64=image_b64, mime_type=mime_type)
        print(description)
        print("-" * 60)
        print("✓ Vision processing succeeded")
    except Exception as exc:
        print(f"✗ Vision processing failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
