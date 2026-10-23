#!/usr/bin/python3
"""Prepare one reusable development userspace on its dedicated data volume."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tarfile
import time
import urllib.request
import zipfile

GIB = 1024 ** 3
PACKAGES = ["ca-certificates", "curl", "git", "openssh-client", "bash", "tini",
            "python3", "python3-venv", "python3-dev", "python3-pip", "build-essential",
            "pkg-config", "cmake", "ninja-build", "jq", "ripgrep", "sqlite3", "rsync",
            "zip", "unzip", "xz-utils", "file", "patch", "procps", "ffmpeg",
            "openjdk-17-jdk-headless", "adb", "fastboot"]


def allocated(root):
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        if Path(base) == root:
            dirs[:] = [d for d in dirs if d not in {"proc", "dev"}]
        for name in dirs + files:
            try:
                total += (Path(base) / name).lstat().st_blocks * 512
            except FileNotFoundError:
                pass
    return total


def verify_digest(path, expected):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 ** 2), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError("checksum mismatch: " + path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--lock", type=Path, default=Path(__file__).with_name("toolchain-lock.json"))
    parser.add_argument("--reserve-gib", type=float, default=20)
    args = parser.parse_args()
    root = args.root.absolute()
    if os.geteuid() != 0 or root.is_symlink() or str(root) in {"/", "/usr", "/opt"}:
        raise ValueError("root and a dedicated, real toolchain directory are required")
    spec_bytes = args.lock.read_bytes()
    spec = json.loads(spec_bytes)
    fingerprint = hashlib.sha256(spec_bytes).hexdigest()
    marker = root / ".usevps-owned"
    manifest = root / "opt/usevps-dev.json"
    if root.exists() and any(root.iterdir()) and not marker.is_file():
        raise ValueError("refusing to modify an existing unowned directory")
    if marker.exists() and marker.read_text().strip() != fingerprint:
        raise ValueError("existing toolchain has a different lock; plan an isolated upgrade")
    if manifest.exists() and json.loads(manifest.read_text())["spec_sha256"] == fingerprint:
        print(json.dumps({"ready": True, "reused": True, "root": str(root)}))
        return
    physical = os.statvfs(root.parent)
    if physical.f_bavail * physical.f_frsize < (args.reserve_gib + 4) * GIB:
        raise RuntimeError("4 GiB installation peak plus the named volume reserve are required")
    root.mkdir(mode=0o755, exist_ok=True)
    marker.write_text(fingerprint + "\n")
    downloads = root / ".downloads"
    downloads.mkdir(exist_ok=True)
    machine = os.uname().machine
    arch = {"aarch64": "arm64", "x86_64": "amd64"}.get(machine)
    if arch is None:
        raise ValueError("unsupported architecture")

    def guard():
        fs = os.statvfs(root)
        if fs.f_bavail * fs.f_frsize < args.reserve_gib * GIB or allocated(root) > 4 * GIB:
            raise RuntimeError("toolchain installation disk budget reached")
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:") and int(line.split()[1]) * 1024 < 2 * GIB:
                raise RuntimeError("host RAM reserve reached during installation")

    def download(url, name, digest):
        path = downloads / name
        if path.exists():
            verify_digest(path, digest)
            return path
        guard()
        tmp = path.with_suffix(path.suffix + ".partial")
        try:
            with urllib.request.urlopen(url, timeout=60) as src, tmp.open("wb") as dst:
                count = 0
                while chunk := src.read(1024 ** 2):
                    count += len(chunk)
                    if count > 256 * 1024 ** 2:
                        raise RuntimeError("download exceeds 256 MiB")
                    dst.write(chunk)
            verify_digest(tmp, digest)
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return path

    env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive", "HOME": "/root",
           "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           "PLAYWRIGHT_BROWSERS_PATH": "/opt/ms-playwright", "npm_config_cache": "/tmp/npm-cache",
           "GRADLE_USER_HOME": "/tmp/gradle-cache", "JAVA_HOME": "/usr/lib/jvm/default-java",
           "PYTHONDONTWRITEBYTECODE": "1"}

    def run(argv, inside=False, timeout=1800):
        guard()
        log = root / ".install-command.log"
        command = ["chroot", str(root), *argv] if inside else argv
        with log.open("wb") as out:
            proc = subprocess.Popen(command, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    guard()
                    if time.monotonic() > deadline or log.stat().st_size > 20 * 1024 ** 2:
                        raise RuntimeError("installation timeout or log limit")
                    time.sleep(2)
            except Exception:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                raise
        if proc.returncode:
            raise RuntimeError(str(argv[:3]) + ": " + log.read_text(errors="replace")[-1800:])
        log.unlink()

    print(json.dumps({"stage": "ubuntu", "arch": arch}), flush=True)
    base = f"ubuntu-base-{spec['ubuntu']}-base-{arch}.tar.gz"
    archive = download("https://cdimage.ubuntu.com/ubuntu-base/releases/24.04/release/" + base,
                       base, spec["ubuntu_sha256"][arch])
    if not (root / "usr/bin/apt-get").exists():
        run(["tar", "-xzf", str(archive), "-C", str(root), "--numeric-owner"])
    for name in ["proc", "dev", "tmp", "opt", "usr/local/bin", "usr/lib64"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "tmp").chmod(0o1777)
    for name, minor in [("null", 3), ("zero", 5), ("random", 8), ("urandom", 9)]:
        path = root / "dev" / name
        if not path.exists():
            os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(1, minor))
        path.chmod(0o666)
    shutil.copyfile("/etc/resolv.conf", root / "etc/resolv.conf")
    (root / "etc/hosts").write_text("127.0.0.1 localhost\n::1 localhost\n")
    (root / "etc/hostname").write_text("usevps\n")
    policy = root / "usr/sbin/policy-rc.d"
    policy.write_text("#!/bin/sh\nexit 101\n")
    policy.chmod(0o755)
    mounted = False
    try:
        if os.path.ismount(root / "proc"):
            raise RuntimeError("unexpected existing proc mount")
        subprocess.run(["mount", "--bind", "/proc", str(root / "proc")], check=True)
        mounted = True
        subprocess.run(["mount", "-o", "remount,bind,ro", str(root / "proc")], check=True)
        run(["apt-get", "update"], inside=True)
        run(["apt-get", "install", "-y", "--no-install-recommends", *PACKAGES], inside=True)
        print(json.dumps({"stage": "node-python-java"}), flush=True)
        node_arch = "arm64" if arch == "arm64" else "x64"
        node_name = f"node-v{spec['node']}-linux-{node_arch}.tar.xz"
        node_tar = download(f"https://nodejs.org/dist/v{spec['node']}/{node_name}", node_name, spec["node_sha256"][arch])
        run(["tar", "-xJf", str(node_tar), "-C", str(root / "usr/local"), "--strip-components=1"])
        run(["npm", "install", "--global", "--prefix", "/usr/local", "--no-audit", "--no-fund",
             "pnpm@" + spec["pnpm"], "playwright-core@" + spec["playwright"]], inside=True)
        run(["python3", "-m", "pip", "install", "--no-cache-dir", "--no-compile", "--target", "/opt/python-tools", "uv==" + spec["uv"]], inside=True)
        uv = root / "usr/local/bin/uv"
        if not uv.is_symlink():
            uv.symlink_to("/opt/python-tools/bin/uv")
        java = next((root / "usr/lib/jvm").glob("java-17-openjdk-*"))
        default_java = root / "usr/lib/jvm/default-java"
        if not default_java.exists():
            default_java.symlink_to(java.name)
        gradle_name = f"gradle-{spec['gradle']}-bin.zip"
        gradle = download("https://services.gradle.org/distributions/" + gradle_name, gradle_name, spec["gradle_sha256"])
        if not (root / ("opt/gradle-" + spec["gradle"])).exists():
            with zipfile.ZipFile(gradle) as z:
                z.extractall(root / "opt")
        (root / ("opt/gradle-" + spec["gradle"] + "/bin/gradle")).chmod(0o755)
        gradle_home = root / "opt/gradle"
        if not gradle_home.is_symlink():
            gradle_home.symlink_to("gradle-" + spec["gradle"])
        gradle_bin = root / "usr/local/bin/gradle"
        if not gradle_bin.is_symlink():
            gradle_bin.symlink_to("/opt/gradle/bin/gradle")
        target = "aarch64-unknown-linux-gnu" if arch == "arm64" else "x86_64-unknown-linux-musl"
        rtk_name = "rtk-" + target + ".tar.gz"
        rtk = download(f"https://github.com/rtk-ai/rtk/releases/download/v{spec['rtk']}/{rtk_name}", rtk_name, spec["rtk_sha256"][arch])
        with tarfile.open(rtk) as tar:
            binary = next(m for m in tar if Path(m.name).name == "rtk" and m.isfile())
            (root / "usr/local/bin/rtk").write_bytes(tar.extractfile(binary).read())
        (root / "usr/local/bin/rtk").chmod(0o755)
        print(json.dumps({"stage": "chromium"}), flush=True)
        run(["node", "/usr/local/lib/node_modules/playwright-core/cli.js", "install-deps", "chromium"], inside=True)
        run(["node", "/usr/local/lib/node_modules/playwright-core/cli.js", "install", "chromium", "--only-shell"], inside=True)
        browser = next(p for p in (root / "opt/ms-playwright").rglob("*") if p.name in {"headless_shell", "chrome-headless-shell"} and p.is_file())
        chromium = root / "usr/local/bin/chromium"
        chromium.write_text('#!/bin/sh\nexec "' + "/" + str(browser.relative_to(root)) + '" "$@"\n')
        chromium.chmod(0o755)
        for path, line in [(root / "etc/passwd", "worker:x:10001:10001:Worker:/workspace:/bin/bash"),
                           (root / "etc/group", "worker:x:10001:")]:
            if ":10001:" not in path.read_text():
                with path.open("a") as f:
                    f.write(line + "\n")
        versions = {}
        for tool, args2 in {"node": ["--version"], "pnpm": ["--version"], "python3": ["--version"],
                            "uv": ["--version"], "java": ["-version"], "gradle": ["--version"],
                            "adb": ["version"], "rtk": ["--version"], "chromium": ["--version"]}.items():
            r = subprocess.run(["chroot", str(root), tool, *args2], env=env, capture_output=True, text=True, timeout=30)
            if r.returncode:
                raise RuntimeError(tool + " failed: " + (r.stdout + r.stderr)[-1000:])
            versions[tool] = (r.stdout + r.stderr).strip()[:250]
        run(["apt-get", "clean"], inside=True)
    finally:
        if mounted:
            subprocess.run(["umount", str(root / "proc")], check=True)
    for path in [downloads, root / "var/lib/apt/lists", root / "tmp"]:
        shutil.rmtree(path)
        path.mkdir(mode=0o1777 if path.name == "tmp" else 0o755)
    result = {"ready": True, "spec_sha256": fingerprint, "arch": arch, "versions": versions,
              "allocated_bytes": allocated(root), "android": "native Java/Gradle/ADB; official Linux x86 NDK/emulator stay on Mac"}
    manifest.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"ready": True, "root": str(root), "allocated_gib": round(result["allocated_bytes"] / GIB, 3), "versions": versions}), flush=True)


if __name__ == "__main__":
    main()
