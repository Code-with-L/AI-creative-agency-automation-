"""Backward-compatible wrapper around ``src.cli``.

The implementation lives in ``src/cli.py`` so it can ship as the
``ai-creative-pipeline`` console entry point. This wrapper keeps the
documented ``python scripts/run_pipeline.py`` invocation working.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
