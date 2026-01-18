#!/usr/bin/env python3
"""Magic Number Audit Script

Phase 10: Proactive code audit to prevent hardcoded values from causing bugs.

This script scans the codebase for:
1. Hardcoded price values (like the $0.53 bug)
2. Suspicious amount/share assignments
3. TODO/FIXME/HACK comments
4. Potential magic numbers in trading logic

Run this before every deploy to catch issues early.

Usage:
    python scripts/audit_magic_numbers.py
    python scripts/audit_magic_numbers.py --strict  # Exit 1 on any warning
"""

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple, NamedTuple
from dataclasses import dataclass


@dataclass
class AuditFinding:
    """Represents a single audit finding."""
    severity: str  # "error", "warning", "info"
    file: Path
    line: int
    message: str
    code: str


class MagicNumberAuditor:
    """Scans codebase for magic numbers and suspicious patterns."""

    # Allowed hardcoded price values
    ALLOWED_PRICES = {
        '0.01',  # Min price / slippage
        '0.02',  # Default slippage
        '0.00',  # Zero
        '0.99',  # Max price
        '0.50',  # Midpoint (common in examples)
        '1.00',  # Dollar
        '1.0',   # Dollar
    }

    # Suspicious patterns to check
    PATTERNS = [
        # Price assignments with literal values
        (r'(?:limit_price|price|yes_price|no_price)\s*=\s*(0\.[0-9]{2})\b',
         'Hardcoded price assignment'),

        # Amount/share assignments
        (r'(?:amount|shares|size)\s*=\s*(\d+\.?\d*)\b',
         'Hardcoded amount/share value'),

        # The specific bug value
        (r'0\.53',
         'THE BUG VALUE ($0.53) - this caused $363 in losses'),
    ]

    # TODO patterns
    TODO_PATTERNS = [
        (r'\b(TODO)\b', 'TODO comment'),
        (r'\b(FIXME)\b', 'FIXME comment'),
        (r'\b(HACK)\b', 'HACK comment'),
        (r'\b(XXX)\b', 'XXX marker'),
        (r'\b(TEMPORARY|TEMP)\b', 'Temporary code marker'),
    ]

    def __init__(self, src_path: Path):
        self.src_path = src_path
        self.findings: List[AuditFinding] = []

    def audit_file(self, filepath: Path) -> None:
        """Audit a single Python file."""
        try:
            content = filepath.read_text()
        except Exception as e:
            self.findings.append(AuditFinding(
                severity="error",
                file=filepath,
                line=0,
                message=f"Could not read file: {e}",
                code="",
            ))
            return

        lines = content.split('\n')

        for i, line in enumerate(lines, 1):
            # Skip empty lines
            if not line.strip():
                continue

            # Check for magic numbers
            self._check_magic_numbers(filepath, i, line)

            # Check for TODO markers
            self._check_todo_markers(filepath, i, line)

    def _check_magic_numbers(self, filepath: Path, line_num: int, line: str) -> None:
        """Check a line for magic numbers."""
        # Skip comments (but still flag TODOs)
        stripped = line.strip()
        if stripped.startswith('#'):
            return

        for pattern, description in self.PATTERNS:
            matches = re.finditer(pattern, line, re.IGNORECASE)
            for match in matches:
                value = match.group(1) if match.lastindex else match.group(0)

                # Check if it's an allowed value
                if value in self.ALLOWED_PRICES:
                    continue

                # Special handling for amounts - allow common init/test values
                if 'amount' in description.lower() or 'share' in description.lower():
                    try:
                        num = float(value)
                        # Allow values that are clearly init/test/config values
                        if num in [0, 0.0, 1, 5, 10, 100, 1000]:
                            continue
                    except ValueError:
                        pass

                # The 0.53 bug is always an error
                if '0.53' in value:
                    severity = "error"
                else:
                    severity = "warning"

                self.findings.append(AuditFinding(
                    severity=severity,
                    file=filepath,
                    line=line_num,
                    message=description,
                    code=line.strip(),
                ))

    def _check_todo_markers(self, filepath: Path, line_num: int, line: str) -> None:
        """Check for TODO/FIXME/HACK markers."""
        for pattern, description in self.TODO_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                self.findings.append(AuditFinding(
                    severity="info",
                    file=filepath,
                    line=line_num,
                    message=description,
                    code=line.strip(),
                ))

    def audit_all(self) -> None:
        """Audit all Python files in the source directory."""
        for filepath in self.src_path.rglob("*.py"):
            # Skip test files for magic number checks (they need test values)
            if 'test' in filepath.name.lower():
                # Still check for 0.53 in test files
                content = filepath.read_text()
                if '0.53' in content:
                    lines = content.split('\n')
                    for i, line in enumerate(lines, 1):
                        if '0.53' in line and not line.strip().startswith('#'):
                            self.findings.append(AuditFinding(
                                severity="error",
                                file=filepath,
                                line=i,
                                message="THE BUG VALUE ($0.53) in test file",
                                code=line.strip(),
                            ))
                continue

            self.audit_file(filepath)

    def report(self) -> Tuple[int, int, int]:
        """Print report and return counts (errors, warnings, info)."""
        errors = [f for f in self.findings if f.severity == "error"]
        warnings = [f for f in self.findings if f.severity == "warning"]
        infos = [f for f in self.findings if f.severity == "info"]

        print("=" * 70)
        print("MAGIC NUMBER AUDIT REPORT")
        print("=" * 70)
        print()

        if errors:
            print(f"\n{'='*20} ERRORS ({len(errors)}) {'='*20}")
            for f in errors:
                print(f"\n  {f.file}:{f.line}")
                print(f"  {f.message}")
                print(f"  > {f.code[:60]}...")

        if warnings:
            print(f"\n{'='*20} WARNINGS ({len(warnings)}) {'='*20}")
            for f in warnings:
                print(f"\n  {f.file}:{f.line}")
                print(f"  {f.message}")
                print(f"  > {f.code[:60]}...")

        if infos:
            print(f"\n{'='*20} INFO ({len(infos)}) {'='*20}")
            for f in infos:
                print(f"\n  {f.file}:{f.line}")
                print(f"  {f.message}")

        print()
        print("=" * 70)
        print(f"SUMMARY: {len(errors)} errors, {len(warnings)} warnings, {len(infos)} info")
        print("=" * 70)

        return len(errors), len(warnings), len(infos)


def main():
    parser = argparse.ArgumentParser(description="Audit codebase for magic numbers")
    parser.add_argument('--strict', action='store_true',
                        help="Exit 1 on any warning or error")
    parser.add_argument('--src', type=str, default='src',
                        help="Source directory to audit")
    args = parser.parse_args()

    src_path = Path(args.src)
    if not src_path.exists():
        print(f"Error: Source path '{src_path}' not found")
        sys.exit(1)

    auditor = MagicNumberAuditor(src_path)
    auditor.audit_all()

    errors, warnings, infos = auditor.report()

    if errors > 0:
        print("\n AUDIT FAILED - Errors found!")
        sys.exit(1)
    elif args.strict and warnings > 0:
        print("\n AUDIT FAILED (strict mode) - Warnings found!")
        sys.exit(1)
    else:
        print("\n AUDIT PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
