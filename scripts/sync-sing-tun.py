#!/usr/bin/env python3
"""Synchronize stable tags and verify or publish pinned development snapshots."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "sing-tun-go"
VERSION_FILE = ROOT / "VERSION"
UPSTREAM_VERSION_FILE = ROOT / "UPSTREAM_VERSION"
UPSTREAM_COMMIT_FILE = ROOT / "UPSTREAM_COMMIT"
UPSTREAM_REPOSITORY = "SagerNet/sing-tun"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPOSITORY}.git"
PROJECT_REPOSITORY = "LorenEteval/sing-tun-python"
PYPI_PROJECT = "sing-tun"
PROJECT_ADDITIONS = frozenset()
RESERVED_PATHS = frozenset({"python-binding", "adapter", "sing_tun", "src"})
EXPECTED_UPSTREAM_GITLINKS: frozenset[str] = frozenset()
UPSTREAM_VERSION_PATTERN = re.compile(
    r"v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\Z"
)
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DEVELOPMENT_VERSION_PATTERN = re.compile(
    r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.dev(?:0|[1-9]\d*)\Z"
)


class SyncError(RuntimeError):
    """A safe, user-facing synchronization failure."""


@dataclass(frozen=True)
class UpstreamCheckout:
    repository: pathlib.Path
    treeish: str
    commit: str


def run(
    command: Sequence[str],
    *,
    cwd: pathlib.Path | None = None,
    input_text: str | None = None,
    text: bool = True,
) -> str | bytes:
    result = subprocess.run(
        command,
        cwd=cwd or ROOT,
        input=input_text,
        capture_output=True,
        text=text,
        encoding="utf-8" if text else None,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() if text else result.stderr.decode().strip()
        raise SyncError(f"Command failed: {' '.join(command)}\n{stderr}")

    return result.stdout


def read_required(path: pathlib.Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise SyncError(f"Missing metadata file: {path}") from error

    if not value:
        raise SyncError(f"Empty metadata file: {path}")

    return value


def parse_upstream_version(tag: str) -> tuple[int, int, int]:
    match = UPSTREAM_VERSION_PATTERN.fullmatch(tag)
    if match is None:
        raise SyncError(f"Expected a stable vX.Y.Z tag, got {tag!r}")

    return tuple(int(part) for part in match.group("version").split("."))


def parse_package_version(version: str) -> tuple[int, int, int]:
    return parse_upstream_version(f"v{version}")


def current_upstream_tag() -> str:
    tag = read_required(UPSTREAM_VERSION_FILE)
    parse_upstream_version(tag)

    return tag


def current_upstream_commit() -> str:
    commit = read_required(UPSTREAM_COMMIT_FILE)
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise SyncError(f"Invalid upstream commit: {commit!r}")

    return commit


def current_package_version() -> str:
    version = read_required(VERSION_FILE)
    parse_package_version(version)
    if version != package_version_for_upstream(current_upstream_tag()):
        raise SyncError("VERSION does not match UPSTREAM_VERSION")

    return version


def current_release_version() -> str:
    version = read_required(VERSION_FILE)
    if DEVELOPMENT_VERSION_PATTERN.fullmatch(version):
        if read_required(UPSTREAM_VERSION_FILE) != "dev":
            raise SyncError("Development VERSION requires UPSTREAM_VERSION=dev")
        current_upstream_commit()
        return version
    return current_package_version()


def current_upstream_reference() -> str:
    version = current_release_version()
    if DEVELOPMENT_VERSION_PATTERN.fullmatch(version):
        return "dev"
    return current_upstream_tag()


def release_provenance() -> str:
    reference = current_upstream_reference()
    if reference == "dev":
        return f"dev ({current_upstream_commit()})"
    return reference


def package_version_for_upstream(tag: str) -> str:
    parse_upstream_version(tag)
    return tag.removeprefix("v")


def downstream_tag(version: str) -> str:
    parse_package_version(version)

    return f"v{version}"


def request_json(url: str, *, missing_ok: bool = False) -> Any | None:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "sing-tun-python-upstream-sync",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(url, headers=headers)

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise SyncError(f"HTTP {error.code} while requesting {url}") from error
    except urllib.error.URLError as error:
        raise SyncError(f"Unable to request {url}: {error.reason}") from error


def stable_release(requested_tag: str | None) -> dict[str, Any]:
    """sing-tun publishes tags, not GitHub Release objects."""
    tags = []
    for page in range(1, 1001):
        values = request_json(
            f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/tags?per_page=100&page={page}"
        )
        if not isinstance(values, list):
            raise SyncError("Invalid upstream tags response")
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                raise SyncError("Malformed upstream tag record")
            tag = value["name"]
            if UPSTREAM_VERSION_PATTERN.fullmatch(tag):
                tags.append(tag)
        if len(values) < 100:
            break
    else:
        raise SyncError("Tag pagination limit exceeded")
    if requested_tag is not None:
        parse_upstream_version(requested_tag)
        if requested_tag not in tags:
            raise SyncError("Requested stable tag does not exist")
        selected = requested_tag
    elif tags:
        selected = max(tags, key=parse_upstream_version)
    else:
        raise SyncError("No stable upstream tags found")
    # Resolve lightweight and arbitrarily nested annotated tags. Never trust
    # pagination's abbreviated or cached commit metadata.
    encoded = urllib.parse.quote(selected, safe="")
    record = request_json(
        f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/git/ref/tags/{encoded}"
    )
    seen = set()
    for _ in range(16):
        obj = record.get("object") if isinstance(record, dict) else None
        if (
            not isinstance(obj, dict)
            or COMMIT_PATTERN.fullmatch(str(obj.get("sha", ""))) is None
        ):
            raise SyncError("Malformed tag object")
        sha = obj["sha"]
        if obj.get("type") == "commit":
            return {"tag_name": selected, "commit": sha}
        if obj.get("type") != "tag" or sha in seen:
            raise SyncError("Invalid or cyclic annotated tag")
        seen.add(sha)
        record = request_json(
            f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}/git/tags/{sha}"
        )
    raise SyncError("Too many annotated tag hops")


def github_resource_exists(endpoint: str) -> bool:
    url = f"https://api.github.com/repos/{PROJECT_REPOSITORY}/{endpoint}"

    return request_json(url, missing_ok=True) is not None


def pypi_version_exists(version: str) -> bool:
    encoded_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{PYPI_PROJECT}/{encoded_version}/json"

    return request_json(url, missing_ok=True) is not None


def published_state(release_tag: str, version: str) -> dict[str, bool]:
    encoded_tag = urllib.parse.quote(release_tag, safe="")

    return {
        "tag": github_resource_exists(f"git/ref/tags/{encoded_tag}"),
        "release": github_resource_exists(f"releases/tags/{encoded_tag}"),
        "pypi": pypi_version_exists(version),
    }


def mapped_downstream_releases(upstream_tag: str) -> list[str]:
    marker = f"Corresponds to sing-tun {upstream_tag}"
    matches: list[str] = []

    for page in range(1, 11):
        releases = request_json(
            f"https://api.github.com/repos/{PROJECT_REPOSITORY}/releases"
            f"?per_page=100&page={page}"
        )
        if not isinstance(releases, list):
            raise SyncError("GitHub returned an invalid downstream releases response")

        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue

            body = release.get("body")
            tag = release.get("tag_name")
            if (
                isinstance(body, str)
                and isinstance(tag, str)
                and marker in body.splitlines()
            ):
                matches.append(tag)

        if len(releases) < 100:
            return sorted(set(matches))

    raise SyncError("Too many downstream releases to verify upstream mappings safely")


def evaluate_release_state(
    *,
    update_required: bool,
    expected_release_tag: str,
    state: dict[str, bool],
    mappings: Sequence[str],
) -> bool:
    conflicting_mappings = sorted(set(mappings) - {expected_release_tag})
    if conflicting_mappings:
        raise SyncError(
            "Upstream release is already mapped to downstream release(s): "
            + ", ".join(conflicting_mappings)
        )

    if update_required:
        if mappings:
            raise SyncError(
                f"Upstream release is already mapped to {expected_release_tag}"
            )
        if any(state.values()):
            occupied = ", ".join(name for name, exists in state.items() if exists)
            raise SyncError(
                f"Release target {expected_release_tag} is occupied by: {occupied}"
            )

        return True

    if any(state.values()) and not all(state.values()):
        present = ", ".join(name for name, exists in state.items() if exists)
        missing = ", ".join(name for name, exists in state.items() if not exists)
        raise SyncError(
            f"Release {expected_release_tag} is inconsistent; "
            f"present: {present}; missing: {missing}"
        )

    if all(state.values()) and expected_release_tag not in mappings:
        raise SyncError("Complete release lacks matching upstream provenance")
    if expected_release_tag in mappings and not state["release"]:
        raise SyncError("Upstream mapping disagrees with release state")

    return not all(state.values())


def write_github_output(path: pathlib.Path | None, values: dict[str, str]) -> None:
    if path is None:
        return

    with path.open("a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise SyncError(f"GitHub output {key!r} contains a newline")
            output.write(f"{key}={value}\n")


@contextlib.contextmanager
def upstream_checkout(
    tag: str, supplied_repository: pathlib.Path | None = None
) -> Iterator[UpstreamCheckout]:
    exact_commit = COMMIT_PATTERN.fullmatch(tag) is not None
    if not exact_commit:
        parse_upstream_version(tag)

    if supplied_repository is not None:
        repository = supplied_repository.resolve()
        commit = str(
            run(["git", "rev-parse", f"{tag}^{{commit}}"], cwd=repository)
        ).strip()
        yield UpstreamCheckout(repository, tag, commit)

        return

    (ROOT / "build").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="sing-tun-upstream-", dir=ROOT / "build"
    ) as raw:
        repository = pathlib.Path(raw)
        run(["git", "init", "--quiet"], cwd=repository)
        run(["git", "remote", "add", "origin", UPSTREAM_URL], cwd=repository)
        run(
            [
                "git",
                "fetch",
                "--quiet",
                "--depth=1",
                "origin",
                tag if exact_commit else f"refs/tags/{tag}",
            ],
            cwd=repository,
        )
        commit = str(
            run(["git", "rev-parse", "FETCH_HEAD^{commit}"], cwd=repository)
        ).strip()
        yield UpstreamCheckout(repository, "FETCH_HEAD", commit)


def upstream_tree(
    checkout: UpstreamCheckout,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    output = run(
        ["git", "ls-tree", "-rz", checkout.treeish],
        cwd=checkout.repository,
        text=False,
    )
    assert isinstance(output, bytes)

    blobs: dict[str, tuple[str, str]] = {}
    gitlinks: set[str] = set()

    for raw_entry in output.split(b"\0"):
        if not raw_entry:
            continue

        metadata, raw_path = raw_entry.split(b"\t", 1)
        mode, object_type, object_hash = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8")

        if object_type == "blob":
            blobs[path] = (mode, object_hash)
        elif mode == "160000" and object_type == "commit":
            gitlinks.add(path)
        else:
            raise SyncError(
                f"Unsupported upstream tree entry {mode} {object_type} at {path}"
            )

    return blobs, gitlinks


def validate_upstream_shape(checkout: UpstreamCheckout) -> dict[str, tuple[str, str]]:
    expected, gitlinks = upstream_tree(checkout)
    for path, (mode, _) in expected.items():
        pure = pathlib.PurePosixPath(path)
        if (
            pure.is_absolute()
            or ".." in pure.parts
            or "\\" in path
            or mode not in {"100644", "100755"}
        ):
            raise SyncError("Unsafe upstream path or mode: " + path)
    if gitlinks != EXPECTED_UPSTREAM_GITLINKS:
        raise SyncError(
            "Upstream gitlinks changed; expected "
            f"{', '.join(sorted(EXPECTED_UPSTREAM_GITLINKS)) or 'none'}; found "
            f"{', '.join(sorted(gitlinks)) or 'none'}"
        )

    collisions = {path for path in expected if path.split("/")[0] in RESERVED_PATHS}
    if collisions:
        raise SyncError(
            "Upstream now owns binding addition paths: " + ", ".join(sorted(collisions))
        )

    return expected


def vendor_files() -> list[str]:
    return sorted(
        path.relative_to(VENDOR_DIR).as_posix()
        for path in VENDOR_DIR.rglob("*")
        if path.is_file() or path.is_symlink()
    )


def working_blob_hashes(paths: Sequence[str]) -> dict[str, str]:
    repository_paths = [f"sing-tun-go/{path}" for path in paths]
    output = str(
        run(
            ["git", "hash-object", "--no-filters", "--stdin-paths"],
            input_text="\n".join(repository_paths) + "\n",
        )
    ).splitlines()
    if len(output) != len(paths):
        raise SyncError("git hash-object returned an unexpected number of hashes")

    return dict(zip(paths, output))


def differs_only_by_checkout_line_endings(
    checkout: UpstreamCheckout, path: str, expected_hash: str
) -> bool:
    actual = (VENDOR_DIR / path).read_bytes()
    expected = run(
        ["git", "cat-file", "blob", expected_hash],
        cwd=checkout.repository,
        text=False,
    )
    assert isinstance(expected, bytes)

    if b"\0" in actual or b"\0" in expected:
        return False

    return actual != expected and actual.replace(b"\r\n", b"\n") == expected.replace(
        b"\r\n", b"\n"
    )


def verify_file_modes(expected: dict[str, tuple[str, str]]) -> None:
    if os.name == "nt":
        print("Skipping executable-mode verification on Windows")

        return

    changed = []
    for path, (expected_mode, _) in expected.items():
        source = VENDOR_DIR / path
        if source.is_symlink():
            actual_mode = "120000"
        elif source.stat().st_mode & stat.S_IXUSR:
            actual_mode = "100755"
        else:
            actual_mode = "100644"

        if actual_mode != expected_mode:
            changed.append(f"{path} ({actual_mode}, expected {expected_mode})")

    if changed:
        raise SyncError("Modified upstream file modes: " + ", ".join(changed))


def verify_vendor(checkout: UpstreamCheckout) -> None:
    expected = validate_upstream_shape(checkout)
    actual_paths = set(vendor_files())
    expected_paths = set(expected).union(PROJECT_ADDITIONS)
    missing = sorted(expected_paths - actual_paths)
    unexpected = sorted(actual_paths - expected_paths)

    if missing or unexpected:
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        raise SyncError("Vendor path mismatch; " + "; ".join(details))

    upstream_paths = sorted(expected)
    actual_hashes = working_blob_hashes(upstream_paths)
    mismatched = [
        path for path in upstream_paths if actual_hashes[path] != expected[path][1]
    ]
    changed = sorted(mismatched)
    if changed:
        raise SyncError("Modified upstream files: " + ", ".join(changed))

    verify_file_modes(expected)
    print(
        f"Verified {len(upstream_paths)} upstream files against {checkout.commit}; "
        f"binding additions: {', '.join(sorted(PROJECT_ADDITIONS))}"
    )


def verify_command(args: argparse.Namespace) -> None:
    current_release_version()
    reference = current_upstream_reference()
    if reference == "dev" and args.tag is not None:
        raise SyncError("Development verification uses the pinned commit, not a tag")
    tag = args.tag or (current_upstream_commit() if reference == "dev" else reference)
    with upstream_checkout(tag, args.upstream_dir) as checkout:
        if (
            reference == "dev" or tag == reference
        ) and checkout.commit != current_upstream_commit():
            raise SyncError(
                f"Upstream tag {tag} resolved to {checkout.commit}, "
                f"not pinned commit {current_upstream_commit()}"
            )
        verify_vendor(checkout)


def safe_extract(archive: pathlib.Path, destination: pathlib.Path) -> None:
    resolved_destination = destination.resolve()
    with tarfile.open(archive) as source:
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if (
                target != resolved_destination
                and resolved_destination not in target.parents
            ):
                raise SyncError(f"Unsafe path in upstream archive: {member.name}")

        if hasattr(tarfile, "data_filter"):
            source.extractall(destination, filter="data")
        else:
            source.extractall(destination)


def export_upstream(checkout: UpstreamCheckout, destination: pathlib.Path) -> None:
    archive = destination.parent / "upstream.tar"
    run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "archive",
            "--format=tar",
            f"--output={archive}",
            checkout.treeish,
        ],
        cwd=checkout.repository,
    )
    destination.mkdir()
    safe_extract(archive, destination)
    archive.unlink()


def ensure_clean_worktree() -> None:
    status = str(run(["git", "status", "--porcelain", "--untracked-files=all"])).strip()
    if status:
        raise SyncError("Synchronization requires a clean Git worktree")


def write_metadata(*, package_version: str, upstream_tag: str, commit: str) -> None:
    parse_package_version(package_version)
    if package_version != package_version_for_upstream(upstream_tag):
        raise SyncError("Package version does not match upstream tag")
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise SyncError(f"Invalid upstream commit: {commit!r}")

    VERSION_FILE.write_text(f"{package_version}\n", encoding="utf-8", newline="\n")
    UPSTREAM_VERSION_FILE.write_text(
        f"{upstream_tag}\n", encoding="utf-8", newline="\n"
    )
    UPSTREAM_COMMIT_FILE.write_text(f"{commit}\n", encoding="utf-8", newline="\n")


def manifest(checkout: UpstreamCheckout) -> str:
    return (
        json.dumps(validate_upstream_shape(checkout), sort_keys=True, indent=2) + "\n"
    )


def adapter_metadata(vendor: pathlib.Path, tag: str) -> tuple[bytes, bytes]:
    mod = (vendor / "go.mod").read_text(encoding="utf-8")
    mod = mod.replace(
        "module github.com/sagernet/sing-tun",
        "module github.com/sagernet/sing-tun/python-adapter",
        1,
    )
    mod += f"\nrequire github.com/sagernet/sing-tun {tag}\n\nreplace github.com/sagernet/sing-tun => ../sing-tun-go\n"
    return mod.encode(), (vendor / "go.sum").read_bytes()


def validate_candidate(candidate: pathlib.Path) -> None:
    """Build/test before promotion, including an installed native-boundary suite."""
    for args in (["go", "test", "-tags", "with_gvisor", "-mod=readonly", "./..."],):
        run(args, cwd=candidate / "adapter")
    run(
        [sys.executable, "-m", "build", "--outdir", str(candidate / "dist")],
        cwd=candidate,
    )
    wheel = next((candidate / "dist").glob("*.whl"))
    env = candidate / "build" / "test-env"
    run([sys.executable, "-m", "venv", str(env)], cwd=candidate)
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)], cwd=env)
    run(
        [str(python), str(candidate / "sample/sample.py"), "--outside", str(candidate)],
        cwd=env,
    )
    # unittest discovery can import fixtures from tests without the candidate
    # package shadowing the installed wheel (cwd is the isolated environment).
    run(
        [
            str(python),
            "-m",
            "unittest",
            "discover",
            "-s",
            str(candidate / "tests"),
            "-p",
            "test_native*.py",
            "-v",
        ],
        cwd=env,
    )


def sync_command(args: argparse.Namespace) -> None:
    ensure_clean_worktree()
    current_package_version()
    remote = stable_release(args.tag) if args.upstream_dir is None else None
    current_tag = current_upstream_tag()
    current_version = parse_upstream_version(current_tag)
    target_version = parse_upstream_version(args.tag)
    if target_version < current_version:
        raise SyncError("Refusing to downgrade upstream")
    with upstream_checkout(current_tag, args.current_upstream_dir) as current:
        if current.commit != current_upstream_commit():
            raise SyncError("Pinned upstream tag moved")
        verify_vendor(current)
        if target_version == current_version:
            if remote and remote["commit"] != current.commit:
                raise SyncError("Pinned upstream tag moved")
            print(f"{current_tag} is already synchronized")
            return
        package = package_version_for_upstream(args.tag)
        with upstream_checkout(args.tag, args.upstream_dir) as target:
            if remote and target.commit != remote["commit"]:
                raise SyncError("Tag moved between discovery and fetch")
            validate_upstream_shape(target)
            (ROOT / "build").mkdir(exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="sing-tun-stage-", dir=ROOT / "build"
            ) as raw:
                stage = pathlib.Path(raw)
                candidate = stage / "candidate"
                candidate.mkdir()
                tracked = str(run(["git", "ls-files"])).splitlines()
                for name in tracked:
                    if name.startswith("sing-tun-go/") or name.startswith(
                        ".codegraph/"
                    ):
                        continue
                    source = ROOT / name
                    dest = candidate / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                export_upstream(target, candidate / "sing-tun-go")
                mod, sums = adapter_metadata(candidate / "sing-tun-go", args.tag)
                updates = {
                    "VERSION": (package + "\n").encode(),
                    "UPSTREAM_VERSION": (args.tag + "\n").encode(),
                    "UPSTREAM_COMMIT": (target.commit + "\n").encode(),
                    "UPSTREAM_TREE.json": manifest(target).encode(),
                    "adapter/go.mod": mod,
                    "adapter/go.sum": sums,
                }
                for name, data in updates.items():
                    (candidate / name).write_bytes(data)
                validate_candidate(candidate)
                # A failed validation has not touched live source. Restore both
                # source and metadata if any operation in promotion fails.
                originals = {name: (ROOT / name).read_bytes() for name in updates}
                backup = stage / "previous-source"
                VENDOR_DIR.rename(backup)
                try:
                    (candidate / "sing-tun-go").rename(VENDOR_DIR)
                    for name, data in updates.items():
                        (ROOT / name).write_bytes(data)
                    verify_vendor(target)
                except BaseException:
                    if VENDOR_DIR.exists():
                        shutil.rmtree(VENDOR_DIR)
                    backup.rename(VENDOR_DIR)
                    for name, data in originals.items():
                        (ROOT / name).write_bytes(data)
                    raise
    print(f"Synchronized {args.tag} ({target.commit}); binding {package}")


def check_release(args: argparse.Namespace) -> None:
    current_package_version()
    current_tag = current_upstream_tag()
    current_version = parse_upstream_version(current_tag)
    release = stable_release(args.tag)
    target_tag = release["tag_name"]
    target_version = parse_upstream_version(target_tag)
    if target_version < current_version:
        raise SyncError(
            f"Upstream target {target_tag} is older than current {current_tag}"
        )

    update_required = target_version > current_version
    package_version = package_version_for_upstream(target_tag)
    release_tag = downstream_tag(package_version)

    with upstream_checkout(target_tag) as checkout:
        if not update_required and checkout.commit != current_upstream_commit():
            raise SyncError(
                f"Upstream tag {target_tag} resolved to {checkout.commit}, "
                f"not pinned commit {current_upstream_commit()}"
            )
        target_commit = checkout.commit
        if target_commit != release["commit"]:
            raise SyncError("Tag moved between discovery and fetch")

    state = published_state(release_tag, package_version)
    mappings = mapped_downstream_releases(target_tag)
    release_required = evaluate_release_state(
        update_required=update_required,
        expected_release_tag=release_tag,
        state=state,
        mappings=mappings,
    )
    values = {
        "current_upstream_tag": current_tag,
        "upstream_tag": target_tag,
        "upstream_commit": target_commit,
        "package_version": package_version,
        "release_tag": release_tag,
        "release_required": str(release_required).lower(),
        "update_required": str(update_required).lower(),
    }
    write_github_output(args.github_output, values)
    print(json.dumps(values, indent=2, sort_keys=True))


def guard_release(args: argparse.Namespace) -> None:
    package_version = current_release_version()
    expected_release_tag = f"v{package_version}"
    if args.release_tag != expected_release_tag:
        raise SyncError(
            f"Release tag {args.release_tag} does not match {VERSION_FILE.name} "
            f"({package_version})"
        )

    upstream_tag = current_upstream_reference()
    if args.upstream_tag is not None and args.upstream_tag != upstream_tag:
        raise SyncError(
            f"Requested upstream tag {args.upstream_tag} does not match "
            f"{UPSTREAM_VERSION_FILE.name} ({upstream_tag})"
        )
    current_upstream_commit()

    state = published_state(args.release_tag, package_version)
    if args.allow_existing_tag:
        if not state["tag"]:
            raise SyncError(f"Expected project tag {args.release_tag} does not exist")
        local_tag_commit = str(
            run(["git", "rev-parse", f"{args.release_tag}^{{commit}}"])
        ).strip()
        head_commit = str(run(["git", "rev-parse", "HEAD^{commit}"])).strip()
        if local_tag_commit != head_commit:
            raise SyncError(
                f"Project tag {args.release_tag} points to {local_tag_commit}, "
                f"not checked out commit {head_commit}"
            )
    elif state["tag"]:
        raise SyncError(f"Project tag {args.release_tag} already exists unexpectedly")

    if state["release"]:
        raise SyncError(
            f"Project GitHub Release {args.release_tag} already exists unexpectedly"
        )
    if state["pypi"]:
        raise SyncError(f"PyPI version {package_version} already exists unexpectedly")

    mappings = mapped_downstream_releases(release_provenance())
    conflicting = sorted(set(mappings) - {args.release_tag})
    if conflicting:
        raise SyncError(
            f"Upstream {upstream_tag} is already mapped to: {', '.join(conflicting)}"
        )

    print(f"Release target {args.release_tag} is available for upstream {upstream_tag}")


def guard_commit(args: argparse.Namespace) -> None:
    current_release_version()
    branch = "codex/development" if current_upstream_reference() == "dev" else "main"
    head = str(run(["git", "rev-parse", "HEAD^{commit}"])).strip()
    if args.checkout_ref:
        if (
            COMMIT_PATTERN.fullmatch(args.checkout_ref) is None
            or args.checkout_ref != head
        ):
            raise SyncError("Checkout ref must be the exact checked out commit")
    run(["git", "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"])
    run(["git", "merge-base", "--is-ancestor", head, f"origin/{branch}"])


def release_notes() -> str:
    return f"Corresponds to sing-tun {release_provenance()}\n"


def release_notes_command(args: argparse.Namespace) -> None:
    notes = release_notes()
    if args.output is None:
        print(notes, end="")
    else:
        args.output.write_text(notes, encoding="utf-8", newline="\n")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)

    check = commands.add_parser("check", help="check for a new stable release")
    check.add_argument("--tag", help="check one explicit stable release tag")
    check.add_argument("--github-output", type=pathlib.Path)
    check.set_defaults(handler=check_release)

    verify = commands.add_parser("verify", help="verify the current vendor tree")
    verify.add_argument("--tag", help="upstream tag (defaults to UPSTREAM_VERSION)")
    verify.add_argument("--upstream-dir", type=pathlib.Path)
    verify.set_defaults(handler=verify_command)

    sync = commands.add_parser("sync", help="synchronize an exact upstream tag")
    sync.add_argument("--tag", required=True)
    sync.add_argument("--upstream-dir", type=pathlib.Path)
    sync.add_argument("--current-upstream-dir", type=pathlib.Path)
    sync.set_defaults(handler=sync_command)

    guard = commands.add_parser(
        "guard-release", help="fail if a release target is inconsistent or occupied"
    )
    guard.add_argument("--release-tag", required=True)
    guard.add_argument("--upstream-tag")
    guard.add_argument("--allow-existing-tag", action="store_true")
    guard.set_defaults(handler=guard_release)

    trusted = commands.add_parser(
        "guard-commit", help="verify release branch and exact commit"
    )
    trusted.add_argument("--checkout-ref")
    trusted.set_defaults(handler=guard_commit)

    notes = commands.add_parser(
        "release-notes", help="write release correspondence metadata"
    )
    notes.add_argument("--output", type=pathlib.Path)
    notes.set_defaults(handler=release_notes_command)

    return result


def main() -> int:
    args = parser().parse_args()

    try:
        args.handler(args)
    except SyncError as error:
        print(f"error: {error}", file=sys.stderr)

        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
