"""
install_hooks.py - installs the pre-commit check (check_no_data_staged.py) into your
local git, so a commit containing private data is refused automatically.
Run once from anywhere in the repo:   python pipeline/scripts/install_hooks.py
"""
import stat
import subprocess
from pathlib import Path

root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())
# Ask git from the repo root, so the (relative) answer is relative to the root.
hook = Path(subprocess.run(["git", "rev-parse", "--git-path", "hooks/pre-commit"],
                           capture_output=True, text=True, check=True, cwd=root).stdout.strip())
hook = hook if hook.is_absolute() else root / hook
existing = hook.read_text() if hook.exists() else ""
marker = "check_no_data_staged.py"
if marker in existing:
    print("Pre-commit check already installed.")
else:
    hook.parent.mkdir(parents=True, exist_ok=True)
    body = existing if existing else "#!/bin/sh\n"
    body += f'\npython3 "{root}/pipeline/scripts/{marker}" || exit 1\n'
    hook.write_text(body)
    hook.chmod(hook.stat().st_mode | stat.S_IEXEC)
    print(f"Installed pre-commit check at {hook}")
