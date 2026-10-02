"""Copy license/notice texts for compiled Go modules into the wheel package."""

import json
import pathlib
import subprocess
import os
import shlex
import shutil
from importlib import metadata

ROOT = pathlib.Path(__file__).resolve().parents[1]


def collect(destination, compiler=None):
    data = subprocess.check_output(
        [
            "go",
            "list",
            "-C",
            str(ROOT / "adapter"),
            "-mod=readonly",
            "-tags",
            "with_gvisor",
            "-deps",
            "-json",
            ".",
        ],
        text=True,
        encoding="utf-8",
    )
    decoder, index, modules = json.JSONDecoder(), 0, {}
    while index < len(data):
        while index < len(data) and data[index].isspace():
            index += 1
        if index == len(data):
            break
        record, index = decoder.raw_decode(data, index)
        module = record.get("Module")
        if module and not module.get("Main"):
            module = module.get("Replace", module)
            modules[module["Path"]] = pathlib.Path(module["Dir"])
    destination.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        command = shlex.split(compiler or os.environ.get("CXX", "g++"), posix=False)[
            0
        ].strip('"')
        executable = shutil.which(command)
        if not executable:
            raise RuntimeError("C++ toolchain unavailable for license collection")
        toolroot = pathlib.Path(executable).resolve().parent.parent
        compiler_notices = [
            p
            for p in toolroot.iterdir()
            if p.is_file()
            and p.name.upper().startswith(("LICENSE", "COPYING", "NOTICE"))
        ]
        for folder in (toolroot / "licenses", toolroot / "share/licenses"):
            if folder.is_dir():
                compiler_notices.extend(p for p in folder.rglob("*") if p.is_file())
        if not compiler_notices:
            raise RuntimeError("Windows compiler license texts missing")
        for path in compiler_notices:
            label = "toolchain_" + path.relative_to(toolroot).as_posix().replace(
                "/", "_"
            )
            (destination / label).write_bytes(path.read_bytes())
    for path in (ROOT / "licenses").iterdir():
        if path.is_file():
            (destination / path.name).write_bytes(path.read_bytes())
    # Preserve file-level notices for upstream's embedded MIT Wintun loader.
    notices = []
    for path in sorted((ROOT / "sing-tun-go/internal/wintun").rglob("*.go")):
        source = path.read_text(encoding="utf-8")
        if source.startswith("/* SPDX-License-Identifier: MIT"):
            notices.append(
                path.relative_to(ROOT).as_posix()
                + "\n"
                + source[: source.index("*/") + 2]
            )
    (destination / "Wintun_loader_NOTICES.txt").write_text(
        "\n\n".join(notices), encoding="utf-8"
    )
    for module, source in modules.items():
        found = False
        for path in source.iterdir():
            if path.is_file() and path.name.upper().startswith(
                ("LICENSE", "COPYING", "NOTICE", "AUTHORS", "PATENTS")
            ):
                name = module.replace("/", "_").replace(".", "_") + "_" + path.name
                (destination / name).write_bytes(path.read_bytes())
                found = True
        if not found:
            raise RuntimeError("dependency license text missing: " + module)
    for label, path in (
        (
            "Go_LICENSE",
            pathlib.Path(
                subprocess.check_output(["go", "env", "GOROOT"], text=True).strip()
            )
            / "LICENSE",
        ),
        ("sing-tun_LICENSE", ROOT / "sing-tun-go/LICENSE"),
    ):
        (destination / label).write_bytes(path.read_bytes())
    dist = metadata.distribution("pybind11")
    files = [
        p
        for p in dist.files
        if "license" in str(p).lower() and str(p).endswith("LICENSE")
    ]
    if not files:
        raise RuntimeError("pybind11 license text unavailable")
    (destination / "pybind11_LICENSE").write_bytes(
        dist.locate_file(files[0]).read_bytes()
    )


if __name__ == "__main__":
    import sys

    collect(pathlib.Path(sys.argv[1]))
