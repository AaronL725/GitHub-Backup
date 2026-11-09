#!/usr/bin/env python3
"""Install reviewed upstream binaries to a private bin directory, verifying pinned hashes."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import platform
import tarfile
import urllib.request
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--bin-dir", type=Path, required=True)
args = parser.parse_args()
arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
if arch is None or platform.system() != "Linux":
    parser.error("Supported platforms: Linux amd64/arm64")
lock = json.loads((Path(__file__).resolve().parents[1] / "deploy/tools.lock.json").read_text())[arch]
args.bin_dir.mkdir(parents=True, exist_ok=True)
for name, item in lock.items():
    print("Installing", name, flush=True)
    blob = urllib.request.urlopen(item["url"], timeout=120).read()
    if hashlib.sha256(blob).hexdigest() != item["sha256"]:
        raise SystemExit("Checksum mismatch: " + name)
    if name == "rclone":
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            binary = archive.read(next(n for n in archive.namelist() if n.endswith("/rclone")))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith("/git-lfs") and m.isfile())
            binary = archive.extractfile(member).read()
    # No extractall: archive path traversal cannot escape this directory.
    dest = args.bin_dir / name
    temp = dest.with_suffix(".new")
    temp.write_bytes(binary)
    temp.chmod(0o755)
    temp.replace(dest)
