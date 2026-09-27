#!/usr/bin/env python3
"""Run offline regression gates without real credentials or model calls."""
import subprocess
import sys
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
for suite in ('test_ccx.py', 'test_ccx_codex.py', 'test_ccx_profile.py',
              'test_ccx_codex_bridge.py', 'test_ccx_runtime.py', 'test-fleet.py',
              'tests/skill-install.py'):
    print('Checking ' + suite, flush=True)
    result = subprocess.run([sys.executable, str(repo / suite)], cwd=repo)
    if result.returncode:
        raise SystemExit(result.returncode)
print('All offline gates passed.')
