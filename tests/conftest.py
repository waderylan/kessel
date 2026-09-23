import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _no_real_provider_clis(monkeypatch):
    """Keep installed Codex and Claude Code CLIs out of reach of unit tests.

    Tests mock provider processes; a PATH lookup that finds a real CLI would
    otherwise make a live, billable request.
    """

    directories = [os.path.dirname(sys.executable)]
    system_root = os.environ.get("SYSTEMROOT")
    if os.name == "nt" and system_root:
        directories.append(os.path.join(system_root, "System32"))
    elif os.name != "nt":
        directories.extend(["/usr/bin", "/bin"])
    monkeypatch.setenv("PATH", os.pathsep.join(directories))
