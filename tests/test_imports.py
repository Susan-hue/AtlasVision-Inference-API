from __future__ import annotations

import subprocess
import sys


def test_api_and_worker_code_never_import_torch() -> None:
    code = (
        "import sys, app.api.main, app.worker.main; "
        "assert 'torch' not in sys.modules, 'torch was imported'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
