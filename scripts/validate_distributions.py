"""Check native wheel tags/machine type and exact vendored sdist contents."""

import hashlib
import json
import pathlib
import struct
import sys
import tarfile
import zipfile


def machine(data):
    if data[:2] == b"MZ":
        offset = struct.unpack_from("<I", data, 0x3C)[0]
        if data[offset : offset + 4] != b"PE\0\0":
            raise RuntimeError("invalid PE native module")
        return {0x8664: "x86_64", 0xAA64: "aarch64"}[
            struct.unpack_from("<H", data, offset + 4)[0]
        ]
    if data[:4] == b"\x7fELF":
        return {62: "x86_64", 183: "aarch64"}[
            struct.unpack_from("<H" if data[5] == 1 else ">H", data, 18)[0]
        ]
    if data[:4] in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf"):
        return {0x1000007: "x86_64", 0x100000C: "aarch64"}[
            struct.unpack_from("<I" if data[0] == 0xCF else ">I", data, 4)[0]
        ]
    raise RuntimeError("unrecognized native machine format")


def validate_wheel(path):
    from packaging.utils import parse_wheel_filename

    name, version, _, tags = parse_wheel_filename(path.name)
    if name != "sing-tun" or any(
        tag.platform == "any" or tag.abi == "none" for tag in tags
    ):
        raise RuntimeError("expected a native sing-tun platform wheel")
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata = [p for p in names if p.endswith(".dist-info/WHEEL")]
        native = [
            p
            for p in names
            if p.startswith("sing_tun/_native.") and p.endswith((".pyd", ".so"))
        ]
        if len(metadata) != 1 or len(native) != 1:
            raise RuntimeError("one WHEEL metadata and one native submodule required")
        wheel_meta = wheel.read(metadata[0]).decode()
        if "Root-Is-Purelib: false" not in wheel_meta:
            raise RuntimeError("pure wheel metadata")
        from packaging.tags import parse_tag

        metadata_tags = set()
        for line in wheel_meta.splitlines():
            if line.startswith("Tag: "):
                metadata_tags.update(parse_tag(line[5:]))
        if metadata_tags != tags:
            raise RuntimeError("wheel filename and metadata tags disagree")
        arch = machine(wheel.read(native[0]))
        if any(
            ("arm64" in tag.platform or "aarch64" in tag.platform)
            != (arch == "aarch64")
            for tag in tags
        ):
            raise RuntimeError("native machine type and platform tag disagree")
        if not any(p.startswith("sing_tun/licenses/") for p in names):
            raise RuntimeError("compiled dependency licenses missing")
        if any(p.startswith(("adapter/", "sing-tun-go/", "build/")) for p in names):
            raise RuntimeError("build source leaked into wheel")
    print(path.name, "native=" + native[0], "machine=" + arch)
    return tags


def validate_sdist(path):
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        if any(
            m.issym()
            or m.islnk()
            or pathlib.PurePosixPath(m.name).is_absolute()
            or ".." in pathlib.PurePosixPath(m.name).parts
            for m in members
        ):
            raise RuntimeError("unsafe sdist archive")
        roots = {pathlib.PurePosixPath(m.name).parts[0] for m in members}
        if len(roots) != 1:
            raise RuntimeError("sdist needs a single root")
        root = roots.pop() + "/"
        entries = {m.name[len(root) :]: m for m in members if m.isfile()}
        required = {
            "adapter/go.mod",
            "adapter/go.sum",
            "LICENSE",
            "UPSTREAM_VERSION",
            "UPSTREAM_COMMIT",
            "UPSTREAM_TREE.json",
            "CMakeLists.txt",
            "src/sing_tun.cpp",
            "sing_tun/api.py",
            "scripts/verify_source.py",
        }
        if not required.issubset(entries):
            raise RuntimeError("sdist is missing native sources/metadata")
        tree = json.load(archive.extractfile(entries["UPSTREAM_TREE.json"]))
        vendored = {
            p[len("sing-tun-go/") :] for p in entries if p.startswith("sing-tun-go/")
        }
        if vendored != set(tree):
            raise RuntimeError("sdist vendor path mismatch")
        for p, (mode, sha) in tree.items():
            member = entries["sing-tun-go/" + p]
            data = archive.extractfile(member).read()
            actual = hashlib.sha1(
                b"blob " + str(len(data)).encode() + b"\0" + data
            ).hexdigest()
            if actual != sha:
                raise RuntimeError("sdist altered upstream source: " + p)
            if bool(member.mode & 0o111) != (mode == "100755"):
                raise RuntimeError("sdist altered upstream mode: " + p)
        if any(
            p.startswith(("build/", ".venv/")) or p.endswith((".pyd", ".so", ".pyc"))
            for p in entries
        ):
            raise RuntimeError("build artifacts leaked into sdist")
    print(path.name, "pristine upstream files:", len(tree))


def validate_matrix(tags):
    expected = set()
    for platform, arch in (
        ("linux", "x86_64"),
        ("linux", "aarch64"),
        ("win", "amd64"),
        ("win", "arm64"),
        ("macosx", "x86_64"),
        ("macosx", "arm64"),
    ):
        for py in ["cp3" + str(n) for n in range(8, 15)] + ["cp313t", "cp314t"]:
            if not (platform == "win" and arch == "arm64" and py == "cp38"):
                expected.add((platform, arch, py))
    actual = set()
    for tag in tags:
        platform = (
            "linux"
            if "manylinux" in tag.platform
            else "win" if tag.platform.startswith("win_") else "macosx"
        )
        arch = (
            "aarch64"
            if platform == "linux" and tag.platform.endswith("aarch64")
            else (
                "arm64"
                if tag.platform.endswith("arm64")
                else "amd64" if platform == "win" else "x86_64"
            )
        )
        py = tag.interpreter + ("t" if tag.abi.endswith("t") else "")
        actual.add((platform, arch, py))
    if expected - actual:
        raise RuntimeError(
            "release wheel matrix incomplete: " + repr(sorted(expected - actual))
        )


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    sdists, wheels = sorted(root.glob("*.tar.gz")), sorted(root.glob("*.whl"))
    if len(sdists) != 1 or not wheels:
        raise RuntimeError("expected one sdist and at least one wheel")
    validate_sdist(sdists[0])
    tags = set()
    for wheel in wheels:
        tags.update(validate_wheel(wheel))
    if "--matrix" in sys.argv:
        validate_matrix(tags)


if __name__ == "__main__":
    main()
