#!/usr/bin/env python3
"""CLI wrapper for parallel validation.

This script provides a command-line interface to run the parallel validation
between Mercury and polyjuiced.

Usage:
    python scripts/parallel_validation.py
    python scripts/parallel_validation.py --output report.json
    python scripts/parallel_validation.py --verbose
"""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from mercury.validation.parallel_validator import main

if __name__ == "__main__":
    sys.exit(main())
