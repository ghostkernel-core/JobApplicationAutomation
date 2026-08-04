"""Keep spawned console programs from opening a window on Windows.

The watcher runs detached under `pythonw.exe`, which has no console of its own.
That is what makes it a background process — and it is also why every console
program it starts flashes a window onto whatever you are looking at: Windows
gives a console application a console, and with no parent console to inherit it
allocates a fresh one. A 45-minute `claude` build therefore parks a cmd window
in front of you for 45 minutes, and each poll cycle's scoring call, each
`taskkill`, and each LaTeX pass inside a build blink one up in turn.

`CREATE_NO_WINDOW` says: run it with no console at all. Correct wherever the
output is captured or redirected — which is every spawn in this project except
the ones deliberately printing to a terminal you are sitting in front of.

    subprocess.run(cmd, capture_output=True, **no_console.kwargs())

On anything other than Windows this is an empty dict, so call sites stay
platform-free.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

# subprocess.CREATE_NO_WINDOW only exists on Windows.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def kwargs(**extra: Any) -> dict[str, Any]:
    """Spawn kwargs that suppress the console window, plus anything passed in.

    `extra` is merged so a caller that already needs its own `creationflags`
    (the watcher's detached start, for one) can combine them rather than pick.
    """
    if sys.platform != "win32":
        return dict(extra)
    flags = int(extra.pop("creationflags", 0)) | CREATE_NO_WINDOW
    return {"creationflags": flags, **extra}
