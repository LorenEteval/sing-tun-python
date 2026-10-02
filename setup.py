# SPDX-License-Identifier: GPL-3.0-or-later
"""PEP 517 native build; all intermediates stay in the build tree."""

import os
import pathlib
import platform
import re
import shlex
import subprocess
import sys
import json
import tarfile
from importlib import metadata

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext
from setuptools.command.sdist import sdist

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from verify_source import package_version, verify
from collect_licenses import collect


class PristineSdist(sdist):
    def make_archive(self, base_name, format, root_dir=None, base_dir=None, **kwargs):
        if format != "gztar":
            return super().make_archive(base_name, format, root_dir, base_dir, **kwargs)
        tree = json.loads((ROOT / "UPSTREAM_TREE.json").read_text())
        filename = base_name + ".tar.gz"
        source = pathlib.Path(root_dir or ".") / (base_dir or ".")

        def modes(member):
            parts = pathlib.PurePosixPath(member.name).parts
            if len(parts) > 2 and parts[1] == "sing-tun-go":
                key = "/".join(parts[2:])
                if key in tree:
                    member.mode = 0o755 if tree[key][0] == "100755" else 0o644
            return member

        with tarfile.open(filename, "w:gz") as archive:
            archive.add(source, arcname=base_dir or source.name, filter=modes)
        return filename


class CMakeBuild(build_ext):
    def build_extension(self, extension):
        verify(ROOT)
        output = pathlib.Path(self.get_ext_fullpath(extension.name)).resolve()
        base = pathlib.Path(self.build_temp).resolve() / extension.name.replace(
            ".", "_"
        )
        go_dir, cmake_dir = base / "go", base / "cmake"
        for path in (output.parent, go_dir, cmake_dir):
            path.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env.setdefault("CGO_ENABLED", "1")
        env.setdefault("GOCACHE", str(ROOT / "build" / "go-cache"))
        env.setdefault("CC", "gcc" if platform.system() == "Windows" else "cc")
        env.setdefault("CXX", "g++" if platform.system() == "Windows" else "c++")
        archs = re.findall(r"-arch\s+(\S+)", env.get("ARCHFLAGS", ""))
        if platform.system() == "Darwin" and archs:
            if len(archs) != 1 or archs[0] not in ("x86_64", "arm64"):
                raise RuntimeError(
                    "build separate macOS architecture wheels; universal Go archives are unsupported"
                )
            env.setdefault("GOARCH", {"x86_64": "amd64", "arm64": "arm64"}[archs[0]])
            if env["GOARCH"] != {"x86_64": "amd64", "arm64": "arm64"}[archs[0]]:
                raise RuntimeError("GOARCH conflicts with ARCHFLAGS")
            env.setdefault("CGO_CFLAGS", "-arch " + archs[0])
            env.setdefault("CGO_LDFLAGS", "-arch " + archs[0])
        subprocess.run(
            [
                "go",
                "build",
                "-C",
                str(ROOT / "adapter"),
                "-mod=readonly",
                "-tags",
                "with_gvisor",
                "-trimpath",
                "-buildmode=c-archive",
                "-ldflags=-s -w -buildid=",
                "-o",
                str(go_dir / "sing_tun.a"),
                ".",
            ],
            env=env,
            check=True,
        )
        args = [
            "cmake",
            "-S",
            str(ROOT),
            "-B",
            str(cmake_dir),
            "-DCMAKE_BUILD_TYPE=Release",
            "-DPYBIND11_FINDPYTHON=ON",
            "-DPython_EXECUTABLE=" + sys.executable,
            "-Dpybind11_DIR="
            + str(
                metadata.distribution("pybind11").locate_file(
                    "pybind11/share/cmake/pybind11"
                )
            ),
            "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY=" + str(output.parent),
            "-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_RELEASE=" + str(output.parent),
            "-DSING_TUN_GO_BUILD_DIR=" + str(go_dir),
        ]
        for name, file in (
            ("VERSION", "VERSION"),
            ("UPSTREAM_VERSION", "UPSTREAM_VERSION"),
            ("UPSTREAM_COMMIT", "UPSTREAM_COMMIT"),
        ):
            args.append("-DSING_TUN_" + name + "=" + (ROOT / file).read_text().strip())
        if platform.system() == "Windows":
            args += ["-G", env.get("CMAKE_GENERATOR", "MinGW Makefiles")]
        if archs:
            args.append("-DCMAKE_OSX_ARCHITECTURES=" + ";".join(archs))
        args += [
            "-DCMAKE_C_COMPILER=" + env["CC"],
            "-DCMAKE_CXX_COMPILER=" + env["CXX"],
        ]
        args += shlex.split(env.get("CMAKE_ARGS", ""))
        subprocess.run(args, env=env, check=True)
        subprocess.run(
            [
                "cmake",
                "--build",
                str(cmake_dir),
                "--config",
                "Release",
                "--parallel",
                str(self.parallel or 2),
            ],
            env=env,
            check=True,
        )
        if not output.is_file():
            raise RuntimeError(
                "CMake did not produce setuptools' expected extension: " + str(output)
            )
        collect(output.parent / "licenses", env["CXX"])


setup(
    name="sing-tun",
    version=package_version(ROOT),
    description="Host-managed sing-tun SOCKS5 bridge for Python",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    license="GPL-3.0-or-later",
    license_files=["LICENSE"],
    url="https://github.com/LorenEteval/sing-tun-python",
    packages=["sing_tun"],
    python_requires=">=3.8",
    ext_modules=[Extension("sing_tun._native", sources=[])],
    cmdclass={"build_ext": CMakeBuild, "sdist": PristineSdist},
    zip_safe=False,
)
