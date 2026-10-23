"""Validate a venv, offline uv installation, and a compiled native library."""
import base64
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

subprocess.run([sys.executable, "-m", "venv", ".venv"], check=True)
files = {"fixturepkg/__init__.py": "answer = 42\n",
         "fixturepkg-0.0.1.dist-info/METADATA": "Metadata-Version: 2.1\nName: fixturepkg\nVersion: 0.0.1\n",
         "fixturepkg-0.0.1.dist-info/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
record = ""
for name, contents in files.items():
    digest = base64.urlsafe_b64encode(hashlib.sha256(contents.encode()).digest()).decode().rstrip("=")
    record += f"{name},sha256={digest},{len(contents.encode())}\n"
files["fixturepkg-0.0.1.dist-info/RECORD"] = record + "fixturepkg-0.0.1.dist-info/RECORD,,\n"
wheel = "fixturepkg-0.0.1-py3-none-any.whl"
with zipfile.ZipFile(wheel, "w") as out:
    for name, contents in files.items():
        out.writestr(name, contents)
subprocess.run(["uv", "pip", "install", "--offline", "--python", ".venv/bin/python", "--no-deps", wheel], check=True)
assert subprocess.check_output([".venv/bin/python", "-c", "import fixturepkg; print(fixturepkg.answer)"], text=True).strip() == "42"
Path("answer.c").write_text("int answer(void) { return 42; }\n")
subprocess.run(["cc", "-shared", "-fPIC", "answer.c", "-o", "answer.so"], check=True)
assert ctypes.CDLL(str(Path("answer.so").resolve())).answer() == 42
proof = {"value": 42, "python": sys.version.split()[0], "venv": True, "uv_offline_install": True, "native_compiler": True}
Path("python-proof.json").write_text(json.dumps(proof))
print(json.dumps(proof))
