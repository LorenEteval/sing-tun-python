"""Offline source ownership check using the committed Git blob manifest."""

import hashlib
import json
import os
import pathlib
import re
import stat

ROOT = pathlib.Path(__file__).resolve().parents[1]


def package_version(root=ROOT):
    version = (root / "VERSION").read_text().strip()
    upstream = (root / "UPSTREAM_VERSION").read_text().strip()
    if (
        re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", upstream)
        is None
        or upstream != "v" + version
    ):
        raise RuntimeError("VERSION must match the stable tag in UPSTREAM_VERSION")
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
