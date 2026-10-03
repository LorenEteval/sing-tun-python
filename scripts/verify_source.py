"""Offline source ownership check using the committed Git blob manifest."""

import hashlib
import json
import os
import pathlib
import re
import stat

ROOT = pathlib.Path(__file__).resolve().parents[1]


STABLE_VERSION = r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"


def package_version(root=ROOT):
    version = (root / "VERSION").read_text().strip()
    upstream = (root / "UPSTREAM_VERSION").read_text().strip()
    development = re.fullmatch(STABLE_VERSION + r"\.dev(?:0|[1-9]\d*)", version)
    if development:
        if upstream != "dev":
            raise RuntimeError("Development VERSION requires UPSTREAM_VERSION=dev")
    elif not re.fullmatch(STABLE_VERSION, version) or upstream != "v" + version:
        raise RuntimeError("VERSION must match the stable tag in UPSTREAM_VERSION")
    commit = (root / "UPSTREAM_COMMIT").read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("UPSTREAM_COMMIT must be an exact commit")
    return version


def verify(root=ROOT):
    package_version(root)
    vendor = root / "sing-tun-go"
    expected = json.loads((root / "UPSTREAM_TREE.json").read_text())
    actual = {
        p.relative_to(vendor).as_posix()
        for p in vendor.rglob("*")
        if p.is_file() or p.is_symlink()
    }
    if actual != set(expected):
        raise RuntimeError("vendored upstream path set differs from the pinned tree")
    for name, (mode, sha) in expected.items():
        p = vendor / name
        if p.is_symlink():
            raise RuntimeError("unexpected upstream symlink: " + name)
        data = p.read_bytes()
        digest = hashlib.sha1(
            b"blob " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        if digest != sha:
            raise RuntimeError("vendored upstream content changed: " + name)
        if os.name != "nt" and bool(p.stat().st_mode & stat.S_IXUSR) != (
            mode == "100755"
        ):
            raise RuntimeError("vendored upstream mode changed: " + name)
    print("Verified", len(expected), "pristine upstream files")


if __name__ == "__main__":
    verify()
