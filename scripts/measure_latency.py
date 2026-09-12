#!/usr/bin/env python3
"""Repeated-measurement latency harness (Plan 2b.8).

Thin wrapper so the operator does not have to ``pip install -e`` the shared
package first. The implementation lives in ``shared.measure``.

    make measure-latency STT_URL=http://127.0.0.1:8000/v1
    python3 scripts/measure_latency.py --replay stt:3.6:0.773,2.655,3.024
"""

from __future__ import annotations

import sys
from pathlib import Path


def _main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "services" / "shared"))
    from shared.measure import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_main())
