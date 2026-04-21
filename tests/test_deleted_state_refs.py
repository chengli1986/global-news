"""Contract test: no Python/shell source should reference deleted state files.

Guards against migration-regression bugs like the 2026-04-21 incident where
unified-global-news-sender.py still read the deleted trial-state.json, silently
zeroing out trial stats and setting up an AUTO-REJECT on the next cycle.

If a migration deletes a shared state file, all readers must be updated — this
test executes scripts/check-deleted-state-refs.sh and fails if any forbidden
reference remains.
"""
import os
import subprocess


def test_no_references_to_deleted_state_files():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "check-deleted-state-refs.sh")
    assert os.path.isfile(script), f"Missing check script: {script}"
    result = subprocess.run([script], capture_output=True, text=True, cwd=repo)
    assert result.returncode == 0, (
        f"Deleted-state-ref check failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
