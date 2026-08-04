"""Test setup: put `automation/` on the path so `watcher` imports as a package.

The watcher is run as `python -m watcher.poll` from `automation/`, not installed,
so there is no package metadata to lean on here.
"""

from __future__ import annotations

import sys
from pathlib import Path

AUTOMATION_DIR = Path(__file__).resolve().parent.parent
if str(AUTOMATION_DIR) not in sys.path:
    sys.path.insert(0, str(AUTOMATION_DIR))
