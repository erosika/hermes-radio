import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# Appended, not prepended: the hermes-agent tree must stay first so its ``tools`` package is not shadowed.
if str(REPO_ROOT) not in sys.path:
    sys.path.append(str(REPO_ROOT))

# mkdtemp() gives a path short enough for a Unix socket; pytest's tmp_path does not on macOS.
_SESSION_HOME = tempfile.mkdtemp(prefix="hermes-radio-test-")
os.environ["HERMES_HOME"] = _SESSION_HOME


@pytest.fixture(scope="session")
def session_home() -> Path:
    return Path(_SESSION_HOME)


@pytest.fixture
def short_home(monkeypatch) -> Path:
    """A fresh HERMES_HOME short enough to hold control.sock."""
    home = Path(tempfile.mkdtemp(prefix="hr-"))
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home
