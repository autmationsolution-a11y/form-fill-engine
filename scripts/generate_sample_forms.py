"""Regenerate the fictional sample PDF forms in samples/forms/.

Usage:  python scripts/generate_sample_forms.py [output_dir]
"""

from __future__ import annotations

import sys
from pathlib import Path

from formfill.sample_forms import build_all

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "samples" / "forms"


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR
    for path in build_all(out_dir):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
