"""_own_python(): pick a system interpreter, never Hermes's, never a dead venv.

Run directly: <system python> test_own_python.py
"""
import os
import sys
from pathlib import Path

import desktop_supervisor as ds


def demo():
    # 1. An explicit override wins outright.
    os.environ["ROUTER_PYTHON"] = r"X:\custom\python.exe"
    assert ds._own_python() == Path(r"X:\custom\python.exe")
    del os.environ["ROUTER_PYTHON"]

    # 2. Whatever we are running on is preferred — and it must not be Hermes's.
    picked = ds._own_python()
    assert picked.exists(), f"picked a nonexistent interpreter: {picked}"
    assert "hermes" not in str(picked).lower(), (
        f"picked Hermes's own python ({picked}) — long-running services there "
        "block `hermes update`")

    # 3. The module-level constant the services actually spawn with agrees.
    assert "hermes" not in str(ds.VENV_PY).lower(), ds.VENV_PY
    assert ds.VENV_PY.exists(), ds.VENV_PY

    # 4. HERMES_PY stays Hermes's, for short-lived `hermes ...` CLI calls only.
    assert "hermes" in str(ds.HERMES_PY).lower()
    print(f"ok (services on {picked})")


if __name__ == "__main__":
    demo()
