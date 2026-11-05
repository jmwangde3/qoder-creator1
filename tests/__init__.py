"""Test package setup.

Unittest drives the real signup/captcha modules, which call write_log(). Keep
that output away from data/qoder.log: tools/captcha_ab.py parses that file and
a phantom "captcha attempt" line from a test looks exactly like a real slider
session.

This module is only imported when unittest is run with the project root as
top-level (`-t .`); `discover -s tests` skips it. src/utils.write_log therefore
also detects a unittest process directly -- do not rely on this hook alone.
Production runs are unaffected (the env var is only set here).
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault(
    "QODER_LOG_FILE",
    str(Path(tempfile.gettempdir()) / "qoder-creator-unittests.log"),
)
