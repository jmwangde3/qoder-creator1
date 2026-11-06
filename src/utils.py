"""
Qoder Creator - Utilities
Logging, random generators, save/load helpers.
"""

import json
import os
import random
import string
import sys
import tempfile
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import LOG_FILE, DATA_DIR


# ================= LOGGING =================
def setup_logging():
    """Setup logging - create log file if not exists."""
    if not LOG_FILE.exists():
        LOG_FILE.write_text(
            f"# Qoder Creator Log - {datetime.now(timezone.utc).isoformat()}\n"
        )


def write_log(message: str, level: str = "INFO"):
    """Write a log entry.

    ``QODER_LOG_FILE`` redirects the stream. Under unittest the stream is
    redirected as well: `discover -s tests` does not import tests/__init__.py,
    so an env var set there would be silently ignored, and fake "Signup start"
    / "captcha attempt" lines would land in the very file that
    tools/captcha_ab.py parses. One test run injected two phantom slider
    sessions into an A/B measurement that way.
    """
    override = os.environ.get("QODER_LOG_FILE")
    if not override and "unittest" in sys.modules:
        override = str(
            Path(tempfile.gettempdir()) / "qoder-creator-unittests.log"
        )
    path = Path(override or LOG_FILE)
    timestamp = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{level}] {message}\n")


# ================= JSONL =================
def save_jsonl(filepath: Path, data: Dict[str, Any]):
    """Append a JSON line to a file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")


def load_jsonl(filepath: Path) -> list:
    """Load all lines from a JSONL file."""
    if not filepath.exists():
        return []
    results = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    results.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return results


# ================= GENERATORS =================
def generate_password(length: int = 14) -> str:
    """Generate a password that satisfies Qoder's policy by construction.

    The rule is: longer than 8 characters, containing a digit, an uppercase
    letter, a lowercase letter and a special character. Picking 14 characters
    uniformly at random misses one of those classes surprisingly often —
    ~11% of draws contain no digit and ~18% no special character — and the
    signup form then refuses to advance with "The password must be longer than
    8 characters and contain numbers, uppercase letters, lowercase letters,
    and special characters". That was 3 of 5 failures in one run.

    So: seed one character from each required class, fill the rest from the
    whole pool, then shuffle so the seeded characters are not in fixed
    positions.
    """
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    special = "!@#$%^&*"

    required = [
        random.choice(upper),
        random.choice(lower),
        random.choice(digits),
        random.choice(special),
    ]

    pool = upper + lower + digits + special
    chars = required + [random.choice(pool) for _ in range(max(0, length - len(required)))]
    random.shuffle(chars)
    return "".join(chars)


def generate_machine_id() -> str:
    """Generate a fake machine ID (32-char hex)."""
    return hashlib.md5(str(random.randint(1000000, 9999999)).encode()).hexdigest()[:32]


# ================= NAMES =================
# Every account used to be registered as "User Dev", which makes a batch
# trivially recognizable. A 40x40 pool gives 1600 combinations; the per-process
# set avoids exact repeats within one run.
_FIRST_NAMES = (
    "Agus", "Andi", "Arif", "Bayu", "Budi", "Candra", "Dedi", "Dimas",
    "Eko", "Fajar", "Galih", "Hadi", "Hendra", "Iwan", "Joko", "Lukman",
    "Nanda", "Rizki", "Samsul", "Slamet", "Sugeng", "Taufik", "Wahyu",
    "Yudi", "Zainal", "Adi", "Farhan", "Gilang", "Indra", "Jaka", "Luthfi",
    "Rahmat", "Rudi", "Surya", "Teguh", "Umar", "Wira", "Yusuf", "Dewi",
    "Siti", "Ayu", "Indah", "Fitri", "Nurul", "Putri", "Anisa", "Dinda",
    "Maya", "Wulan", "Yuni", "Ratna", "Endah", "Ika", "Nia", "Rani",
    "Lestari", "Permata",
)
_LAST_NAMES = (
    "Santoso", "Wijaya", "Pratama", "Hidayat", "Nugroho", "Saputra",
    "Setiawan", "Kurniawan", "Firmansyah", "Ramadhan", "Siregar",
    "Simanjuntak", "Hutapea", "Nasution", "Lubis", "Gunawan", "Halim",
    "Rahman", "Ismail", "Suryana", "Permana", "Kusuma", "Wibowo", "Hartono",
    "Susanto", "Budiman", "Sudirman", "Rahayu", "Utami", "Handoko", "Irawan",
    "Prasetyo", "Yulianto", "Arifin", "Fadillah", "Harahap", "Sinaga",
    "Purnama", "Sihombing", "Pangestu",
)
_USED_NAMES: set = set()


def generate_name() -> tuple:
    """Return a varied first/last name pair for the signup form."""
    first = last = ""
    for _ in range(30):
        first = random.choice(_FIRST_NAMES)
        last = random.choice(_LAST_NAMES)
        if first == last:
            continue
        full = f"{first} {last}"
        if full not in _USED_NAMES:
            _USED_NAMES.add(full)
            return first, last
    # Pool exhausted or unlucky; a repeat is better than blocking a signup.
    return first, last


def generate_ms_deviceid() -> str:
    """Generate a fake Microsoft device ID."""
    return str(uuid.uuid4())


def generate_nonce(length: int = 16) -> str:
    """Generate a random nonce."""
    return uuid.uuid4().hex[:length]


# ================= TEXT HELPERS =================
def extract_otp(text: str, length: int = 6) -> Optional[str]:
    """Extract OTP/verification code from text."""
    import re
    patterns = [
        rf'\b(\d{{{length}}})\b',
        r'verification code[:\s]+(\d{4,8})',
        r'code[:\s]+(\d{4,8})',
        r'OTP[:\s]+(\d{4,8})',
        r'one.time.passcode[:\s]+(\d{4,8})',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None