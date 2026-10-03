"""Stable tag and transactional source-sync tests with local Git fixtures."""

import argparse
import contextlib
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "sync_sing_tun", ROOT / "scripts/sync-sing-tun.py"
)
sync = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sync
spec.loader.exec_module(sync)


def git(root, *args):
    return subprocess.check_output(
        ["git", *args], cwd=root, text=True, stderr=subprocess.STDOUT
    ).strip()


def write(root, name, data):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data, encoding="utf-8")


def init(root):
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")


def commit(root):
    git(root, "add", "--all")
    git(root, "commit", "-qm", "fixture source")


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory() as raw:
        base = pathlib.Path(raw)
        upstream, project = base / "upstream", base / "project"
        init(upstream)
        write(upstream, "go.mod", "module github.com/sagernet/sing-tun\n\ngo 1.25.0\n")
        write(upstream, "go.sum", "")
        write(upstream, "tun.go", "package tun\n")
        commit(upstream)
        git(upstream, "tag", "v0.9.6")
        pin = git(upstream, "rev-parse", "HEAD")
        write(upstream, "new.go", "package tun\n")
        commit(upstream)
        git(upstream, "tag", "-a", "v0.10.0", "-m", "annotated stable")
        init(project)
        with sync.upstream_checkout("v0.9.6", upstream) as checkout:
            sync.export_upstream(checkout, project / "sing-tun-go")
            write(project, "UPSTREAM_TREE.json", sync.manifest(checkout))
        for name, value in (
            ("VERSION", "0.9.6\n"),
            ("UPSTREAM_VERSION", "v0.9.6\n"),
            ("UPSTREAM_COMMIT", pin + "\n"),
            ("adapter/go.mod", "original adapter\n"),
            ("adapter/go.sum", "original sum\n"),
            ("adapter/engine.go", "package main\n"),
        ):
            write(project, name, value)
        write(project, ".gitignore", "build/\n")
        commit(project)
        with mock.patch.multiple(
            sync,
            ROOT=project,
            VENDOR_DIR=project / "sing-tun-go",
            VERSION_FILE=project / "VERSION",
            UPSTREAM_VERSION_FILE=project / "UPSTREAM_VERSION",
            UPSTREAM_COMMIT_FILE=project / "UPSTREAM_COMMIT",
        ):
            yield upstream, project


def args(upstream, tag="v0.10.0"):
    return argparse.Namespace(
        tag=tag,
        upstream_dir=upstream,
        current_upstream_dir=upstream,
    )


class SyncTests(unittest.TestCase):
    def test_development_provenance_and_stable_sync_isolation(self):
        with fixture() as (upstream, project):
            write(project, "VERSION", "0.9.7.dev0\n")
            write(project, "UPSTREAM_VERSION", "dev\n")
            self.assertEqual(sync.current_release_version(), "0.9.7.dev0")
            self.assertEqual(sync.current_upstream_reference(), "dev")
            pin = sync.current_upstream_commit()
            self.assertEqual(
                sync.release_notes(), f"Corresponds to sing-tun dev ({pin})\n"
            )
            # Verification uses the recorded SHA even when dev/HEAD has moved.
            sync.verify_command(argparse.Namespace(tag=None, upstream_dir=upstream))
            commit(project)
            with mock.patch.object(
                sync, "stable_release"
            ) as discover, self.assertRaises(sync.SyncError):
                sync.check_release(argparse.Namespace(tag=None, github_output=None))
            discover.assert_not_called()
            with self.assertRaises(sync.SyncError):
                sync.sync_command(args(upstream))
            with self.assertRaises(sync.SyncError):
                sync.verify_command(
                    argparse.Namespace(tag="v0.9.6", upstream_dir=upstream)
                )
            write(project, "UPSTREAM_VERSION", "v0.9.6\n")
            with self.assertRaisesRegex(sync.SyncError, "requires"):
                sync.current_release_version()
            write(project, "UPSTREAM_VERSION", "dev\n")
            write(project, "UPSTREAM_COMMIT", "dev\n")
            with self.assertRaisesRegex(sync.SyncError, "commit"):
                sync.current_release_version()

    def test_development_release_target_and_mapping(self):
        empty = {"tag": False, "release": False, "pypi": False}
        with fixture() as (upstream, project), mock.patch.object(
            sync, "published_state", return_value=empty
        ), mock.patch.object(
            sync, "mapped_downstream_releases", return_value=[]
        ) as mappings:
            write(project, "VERSION", "0.9.7.dev0\n")
            write(project, "UPSTREAM_VERSION", "dev\n")
            target = argparse.Namespace(
                release_tag="v0.9.7.dev0", upstream_tag="dev", allow_existing_tag=False
            )
            sync.guard_release(target)
            mappings.assert_called_with(f"dev ({sync.current_upstream_commit()})")
            target.release_tag = "v0.9.7"
            with self.assertRaisesRegex(sync.SyncError, "does not match"):
                sync.guard_release(target)
            target.release_tag = "v0.9.7.dev0"
            target.upstream_tag = "v0.9.7"
            with self.assertRaisesRegex(sync.SyncError, "does not match"):
                sync.guard_release(target)

    def test_development_release_requires_trusted_branch_and_exact_checkout(self):
        with fixture() as (upstream, project):
            write(project, "VERSION", "0.9.7.dev0\n")
            write(project, "UPSTREAM_VERSION", "dev\n")
            commit(project)
            git(project, "branch", "codex/development")
            git(project, "remote", "add", "origin", str(project))
            head = git(project, "rev-parse", "HEAD")
            sync.guard_commit(argparse.Namespace(checkout_ref=head))
            with self.assertRaisesRegex(sync.SyncError, "exact"):
                sync.guard_commit(argparse.Namespace(checkout_ref="codex/development"))
            write(project, "extra", "untrusted commit")
            commit(project)
            with self.assertRaises(sync.SyncError):
                sync.guard_commit(argparse.Namespace(checkout_ref=None))

    def test_pagination_numeric_order_and_annotated_tag(self):
        calls = []

        def response(url):
            calls.append(url)
            if "&page=1" in url:
                return [{"name": "v0.9.9"}] * 99 + [{"name": "v99.0.0-beta.1"}]
            if "&page=2" in url:
                return [{"name": "v0.10.0"}, {"name": "v01.0.0"}]
            if "/git/ref/" in url:
                return {"object": {"type": "tag", "sha": "a" * 40}}
            return {"object": {"type": "commit", "sha": "b" * 40}}

        with mock.patch.object(sync, "request_json", side_effect=response):
            self.assertEqual(
                sync.stable_release(None), {"tag_name": "v0.10.0", "commit": "b" * 40}
            )
            self.assertTrue(any("page=2" in url for url in calls))
            self.assertFalse(any("/releases" in url for url in calls))
            self.assertEqual(sync.stable_release("v0.10.0")["commit"], "b" * 40)
            with self.assertRaises(sync.SyncError):
                sync.stable_release("v99.0.0-beta.1")

    def test_bad_manual_tag_and_cyclic_object(self):
        for tag in ("main", "v1.0.0-rc.1", "v01.2.3", "v1.2", "v1.2.3\n"):
            with self.assertRaises(sync.SyncError):
                sync.parse_upstream_version(tag)
        responses = [
            [{"name": "v1.0.0"}],
            {"object": {"type": "tag", "sha": "a" * 40}},
            {"object": {"type": "tag", "sha": "a" * 40}},
        ]
        with mock.patch.object(
            sync, "request_json", side_effect=responses
        ), self.assertRaisesRegex(sync.SyncError, "cyclic"):
            sync.stable_release(None)

    def test_sync_idempotent_and_upstream_package_version(self):
        with fixture() as (upstream, project), mock.patch.object(
            sync, "validate_candidate"
        ) as validate:
            sync.sync_command(args(upstream))
            self.assertEqual((project / "VERSION").read_text(), "0.10.0\n")
            self.assertEqual((project / "UPSTREAM_VERSION").read_text(), "v0.10.0\n")
            self.assertEqual(
                (project / "adapter/engine.go").read_text(), "package main\n"
            )
            validate.assert_called_once()
            commit(project)
            sync.sync_command(args(upstream, "v0.10.0"))
            validate.assert_called_once()
            self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_dirty_downgrade_moved_tag_and_collision(self):
        with fixture() as (upstream, project):
            write(project, "untracked", "dirty")
            with self.assertRaisesRegex(sync.SyncError, "clean"):
                sync.sync_command(args(upstream))
            (project / "untracked").unlink()
            with self.assertRaisesRegex(sync.SyncError, "downgrade"):
                sync.sync_command(args(upstream, "v0.8.0"))
            git(upstream, "tag", "-f", "v0.9.6", "HEAD")
            with self.assertRaisesRegex(sync.SyncError, "moved"):
                sync.sync_command(args(upstream))
        with fixture() as (upstream, project):
            write(upstream, "python-binding/main.go", "package main\n")
            commit(upstream)
            git(upstream, "tag", "v0.11.0")
            with self.assertRaisesRegex(sync.SyncError, "binding"):
                sync.sync_command(args(upstream, "v0.11.0"))
            self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_content_addition_deletion_and_modes(self):
        for kind in ("change", "add", "delete", "symlink", "mode"):
            with self.subTest(kind=kind), fixture() as (upstream, project):
                p = project / "sing-tun-go/tun.go"
                if kind == "change":
                    p.write_text("changed\n")
                elif kind == "add":
                    write(project, "sing-tun-go/add.go", "addition")
                elif kind == "delete":
                    p.unlink()
                elif kind == "mode":
                    if sys.platform == "win32":
                        continue  # Windows has no executable bit.
                    p.chmod(0o755)
                else:
                    # Git fixture symlink modes are rejected even on Windows.
                    git(
                        upstream,
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        "120000," + git(upstream, "rev-parse", "HEAD:tun.go") + ",link",
                    )
                    with self.assertRaises(sync.SyncError):
                        tree = git(upstream, "write-tree")
                        sync.validate_upstream_shape(
                            sync.UpstreamCheckout(upstream, tree, "0" * 40)
                        )
                    continue
                with sync.upstream_checkout(
                    "v0.9.6", upstream
                ) as checkout, self.assertRaises(sync.SyncError):
                    sync.verify_vendor(checkout)

    def test_failed_candidate_does_not_replace_source(self):
        with fixture() as (upstream, project):
            before = (project / "UPSTREAM_COMMIT").read_bytes()
            with mock.patch.object(
                sync, "validate_candidate", side_effect=sync.SyncError("build failed")
            ), self.assertRaises(sync.SyncError):
                sync.sync_command(args(upstream))
            self.assertEqual((project / "UPSTREAM_COMMIT").read_bytes(), before)
            self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_promotion_failure_restores_every_metadata_file(self):
        with fixture() as (upstream, project):
            originals = {
                p.relative_to(project): p.read_bytes()
                for p in project.rglob("*")
                if p.is_file() and ".git" not in p.parts
            }
            verifier = sync.verify_vendor
            count = 0

            def fail_second(checkout):
                nonlocal count
                count += 1
                if count == 2:
                    raise sync.SyncError("post-promotion failure")
                verifier(checkout)

            with mock.patch.object(sync, "validate_candidate"), mock.patch.object(
                sync, "verify_vendor", side_effect=fail_second
            ), self.assertRaises(sync.SyncError):
                sync.sync_command(args(upstream))
            for name, data in originals.items():
                self.assertEqual((project / name).read_bytes(), data)
            self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_release_guards(self):
        empty = {"tag": False, "release": False, "pypi": False}
        full = {key: True for key in empty}
        evaluate = sync.evaluate_release_state
        self.assertTrue(
            evaluate(
                update_required=True,
                expected_release_tag="v0.10.0",
                state=empty,
                mappings=[],
            )
        )
        self.assertFalse(
            evaluate(
                update_required=False,
                expected_release_tag="v0.10.0",
                state=full,
                mappings=["v0.10.0"],
            )
        )
        for state, mappings, update in (
            (full, [], True),
            (full, [], False),
            (empty, ["v0.10.0"], False),
            ({**empty, "tag": True}, [], False),
            (empty, ["v0.9.6"], False),
            (empty, ["v0.10.0"], True),
        ):
            with self.assertRaises(sync.SyncError):
                evaluate(
                    update_required=update,
                    expected_release_tag="v0.10.0",
                    state=state,
                    mappings=mappings,
                )

    def test_reject_independent_version_before_sync(self):
        with fixture() as (upstream, project), mock.patch.object(
            sync, "validate_candidate"
        ) as validate:
            write(project, "VERSION", "0.1.0\n")
            commit(project)
            before = (project / "UPSTREAM_COMMIT").read_bytes()
            with self.assertRaisesRegex(sync.SyncError, "does not match"):
                sync.sync_command(args(upstream))
            self.assertEqual((project / "UPSTREAM_COMMIT").read_bytes(), before)
            validate.assert_not_called()
            self.assertEqual(git(project, "status", "--porcelain"), "")

    def test_guard_metadata_and_occupied_target(self):
        empty = {"tag": False, "release": False, "pypi": False}
        with fixture() as (upstream, project), mock.patch.object(
            sync, "published_state", return_value=empty
        ), mock.patch.object(sync, "mapped_downstream_releases", return_value=[]):
            target = argparse.Namespace(
                release_tag="v0.9.6", upstream_tag="v0.9.6", allow_existing_tag=False
            )
            sync.guard_release(target)
            notes = sync.release_notes()
            self.assertEqual(notes, "Corresponds to sing-tun v0.9.6\n")
            target.release_tag = "v9.0.0"
            with self.assertRaisesRegex(sync.SyncError, "does not match"):
                sync.guard_release(target)
            target.release_tag = "v0.9.6"
            target.upstream_tag = "v0.9.5"
            with self.assertRaisesRegex(sync.SyncError, "does not match"):
                sync.guard_release(target)
            target.upstream_tag = "v0.9.6"
            with mock.patch.object(
                sync, "published_state", return_value={**empty, "pypi": True}
            ), self.assertRaisesRegex(sync.SyncError, "PyPI"):
                sync.guard_release(target)


if __name__ == "__main__":
    unittest.main()
