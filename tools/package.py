"""Build the one-file Raspberry Pi installer (runs on any OS, standard library only).

    python tools/package.py            -> dist/smart-smoke-detector-<version>-installer.sh  (~45 KB)
    python tools/package.py --offline  -> dist/smart-smoke-detector-<version>-offline-installer.sh
                                          (~90 MB: bundles the aarch64 Python wheels, so pip needs
                                          no internet; apt still does unless its packages are there)

On the Pi, run it once as the desktop user:   bash smart-smoke-detector-<version>-installer.sh
It unpacks to ~/smart-smoke-detector, runs install_pi.sh and starts the app.

The file is tools/installer_header.sh followed by a gzipped tar of the app (checked with SHA-256
before unpacking). Shell and Python files are written with LF line endings and the scripts marked
executable, so an installer built on Windows still runs on the Pi.
"""

import argparse
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("requirements.txt", "run_pi.sh", "install_pi.sh", "README.md")
EXECUTABLE = {"run_pi.sh", "install_pi.sh"}
# Python versions of the supported Raspberry Pi OS releases: Bookworm 3.11, Trixie 3.13.
# (Only dbus-fast is version specific; PySide6 is abi3 and bleak is pure Python.)
PI_PYTHONS = ("3.11", "3.13")
PI_PLATFORMS = ("manylinux_2_31_aarch64", "manylinux_2_28_aarch64", "manylinux_2_17_aarch64",
                "manylinux2014_aarch64")


def version() -> str:
    with open(os.path.join(ROOT, "app", "__init__.py"), encoding="utf-8") as f:
        return re.search(r'__version__ = "([^"]+)"', f.read()).group(1)


def sources():
    for name in FILES:
        yield name
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "app")):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in sorted(filenames):
            if fn.endswith(".py"):
                yield os.path.relpath(os.path.join(dirpath, fn), ROOT).replace(os.sep, "/")


def pinned() -> list:
    """Requirement pins, markers dropped: wheels are fetched for the Pi, not for this machine."""
    reqs = []
    with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].split(";", 1)[0].strip()
            if line:
                reqs.append(line)
    pyside = next(r for r in reqs if r.lower().startswith("pyside6-essentials=="))
    reqs.append("shiboken6==" + pyside.split("==", 1)[1])
    reqs.append("typing-extensions")  # bleak needs it on Python < 3.12
    return reqs


def download_wheels(dest: str) -> None:
    # --no-deps with an explicit list: pip would otherwise resolve markers for *this* machine
    # (e.g. pull Windows-only winrt packages and skip dbus-fast).
    for py in PI_PYTHONS:
        cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
               "--implementation", "cp", "--python-version", py, "--dest", dest, "--quiet"]
        for plat in PI_PLATFORMS:
            cmd += ["--platform", plat]
        print(f"downloading aarch64 wheels for Python {py} ...")
        subprocess.run(cmd + pinned(), check=True)


def read_lf(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read()
    return data.replace(b"\r\n", b"\n") if path.endswith((".sh", ".py", ".txt", ".md")) else data


def add_file(tar: tarfile.TarFile, src: str, arcname: str, mode: int) -> None:
    data = read_lf(src)
    info = tarfile.TarInfo(arcname)
    info.size, info.mode, info.mtime = len(data), mode, int(os.path.getmtime(src))
    tar.addfile(info, io.BytesIO(data))


def payload(name: str, offline: bool) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in sources():
            add_file(tar, os.path.join(ROOT, rel), f"{name}/{rel}",
                     0o755 if os.path.basename(rel) in EXECUTABLE else 0o644)
        if offline:
            with tempfile.TemporaryDirectory() as tmp:
                download_wheels(tmp)
                for fn in sorted(os.listdir(tmp)):
                    add_file(tar, os.path.join(tmp, fn), f"{name}/wheels/{fn}", 0o644)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="bundle the aarch64 wheels")
    args = ap.parse_args()

    ver = version()
    data = payload(f"smart-smoke-detector-{ver}", args.offline)
    header = read_lf(os.path.join(ROOT, "tools", "installer_header.sh")).decode()
    header = header.replace("@VERSION@", ver).replace("@SHA256@", hashlib.sha256(data).hexdigest())
    assert header.endswith("\n__PAYLOAD_BELOW__\n")

    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    kind = "offline-installer" if args.offline else "installer"
    out = os.path.join(ROOT, "dist", f"smart-smoke-detector-{ver}-{kind}.sh")
    with open(out, "wb") as f:
        f.write(header.encode() + data)
    try:
        os.chmod(out, 0o755)
    except OSError:
        pass
    print(f"{out}  ({os.path.getsize(out) / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
