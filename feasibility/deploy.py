"""Deploy a new immutable copy to the Mac; never overwrite or delete files."""
import ast
from datetime import datetime, timezone
from pathlib import Path
import shlex
import subprocess

source = Path(__file__).with_name("lab.py").read_bytes()
ast.parse(source)
name = "lab-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".py"
remote = f"""
import sys
from pathlib import Path
directory = Path.home() / 'mami-lab/code'
directory.mkdir(exist_ok=True)
path = directory / {name!r}
with path.open('xb') as f:
    f.write(sys.stdin.buffer.read())
print(path)
"""
subprocess.run(
    ["ssh", "-o", "BatchMode=yes", "mami-mac",
     '"$HOME/mami-lab/.venv/bin/python" -c ' + shlex.quote(remote)],
    input=source, check=True,
)
