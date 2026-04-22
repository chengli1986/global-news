"""Contract test: multi-line shell variable assignments must have a runtime guard.

Guards against the 2026-04-22 incident where rss-source-discovery.sh was broken
by an unescaped inner double quote inside the multi-line PROMPT assignment.
bash parsed it as a command-prefix assignment (VAR=value cmd), silently never
setting the shell variable, and the cron crashed ~100 lines later with an
opaque "unbound variable" error. A one-line `: "${VAR:?...}"` guard
immediately after the assignment turns that into a clear, single-error failure.

This test executes scripts/check-shell-prompt-assignments.sh and fails if any
qualifying multi-line assignment is missing its guard.
"""
import os
import subprocess


def test_shell_multiline_assignments_have_guards():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = os.path.join(repo, "scripts", "check-shell-prompt-assignments.sh")
    assert os.path.isfile(script), f"Missing check script: {script}"
    result = subprocess.run([script], capture_output=True, text=True, cwd=repo)
    assert result.returncode == 0, (
        f"Shell multi-line assignment guard check failed:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
