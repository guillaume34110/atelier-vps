"""Compile and run a real Gradle JVM application without external dependencies."""
import json
from pathlib import Path
import subprocess

Path("src/main/java").mkdir(parents=True)
Path("src/main/java/Answer.java").write_text('public class Answer { public static void main(String[] args) { System.out.println(42); } }\n')
Path("settings.gradle").write_text("rootProject.name='usevps-check'\n")
Path("build.gradle").write_text("plugins { id 'application' }\napplication { mainClass = 'Answer' }\n")
r = subprocess.run(["gradle", "--offline", "--no-daemon", "--console=plain", "-q", "run"], text=True, capture_output=True, timeout=120)
assert r.returncode == 0 and "42" in r.stdout.splitlines(), (r.stdout[-1000:], r.stderr[-1000:])
adb = subprocess.check_output(["adb", "version"], text=True)
assert "Android Debug Bridge" in adb
proof = {"value": 42, "gradle_jvm_build": True, "adb_client": True}
Path("java-proof.json").write_text(json.dumps(proof))
print(json.dumps(proof))
