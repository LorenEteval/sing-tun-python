# sing-tun-python

[![Deploy PyPI](https://github.com/LorenEteval/sing-tun-python/actions/workflows/deploy-pypi.yml/badge.svg?branch=main)](https://github.com/LorenEteval/sing-tun-python/actions/workflows/deploy-pypi.yml)

Python bindings for [sing-tun](https://github.com/SagerNet/sing-tun), with a SOCKS5 forwarding handler.

## Installation

Install from PyPI:

```console
pip install sing-tun==0.9.7.dev0
```

Binary wheels include the compiled Go backend and native Python binding. Installing a compatible wheel does not require
Go, CMake, or a C/C++ compiler.

### Binary Wheel Support

The GitHub Actions build matrix targets:

| Platform | Architecture | CPython versions |
|----------|--------------|------------------|
| Linux (manylinux2014) | x86_64 | 3.8-3.14, 3.13t, 3.14t |
| Linux (manylinux2014) | ARM64 | 3.8-3.14, 3.13t, 3.14t |
| Windows | x86_64 | 3.8-3.14, 3.13t, 3.14t |
| Windows | ARM64 | 3.9-3.14, 3.13t, 3.14t |
| macOS | Intel | 3.8-3.14, 3.13t, 3.14t |
| macOS | Apple Silicon | 3.8-3.14, 3.13t, 3.14t |

Windows ARM64 starts at Python 3.9. Free-threaded wheels retain CPython's compatibility GIL.

### Building from Source

Building from source requires:

- [Go](https://go.dev/doc/install) at least the version in `adapter/go.mod`
- [CMake](https://cmake.org/download/) 3.18 or newer
- A C++17 toolchain: GCC on Linux, Apple Clang on macOS, MinGW-w64 on Windows x86_64, or LLVM-MinGW on Windows ARM64

```console
pip install .
```

PEP 517 installs setuptools, CMake and pybind11 build dependencies. Go module dependencies may still need downloading.
Build intermediates stay under `build/`. macOS builds support one `ARCHFLAGS` architecture per wheel.

## API

```python
from sing_tun import Config, Engine

config = Config(
    proxy="socks5://127.0.0.1:1080",
    stack="go",
    tun_options={
        "MTU": 1500,
        "Inet4Address": ["198.18.0.1/15"],
        "Inet6Address": ["fd00::1/64"],
    },
    stack_options={"UDPTimeout": "1m", "ICMPTimeout": "10s", "UDPNATMax": 4096},
)

with Engine(config) as engine:
    assert engine.ready
    # Another thread or a child control loop can call engine.stop().
    engine.wait()
```

`tun_options` and `stack_options` use the exported field names of upstream
[`tun.Options`](sing-tun-go/tun.go) and [`tun.StackOptions`](sing-tun-go/stack.go), respectively.
JSON-compatible fields pass directly into those structs. Addresses and prefixes use strings, NAT mapping/filtering
values use upstream's numeric enums (0, 1, 2), and duration fields accept Go duration strings or integer nanoseconds.
Unknown fields and incorrect types are rejected. Runtime objects such as Context, Tun, Handler, Logger,
InterfaceFinder and InterfaceMonitor are supplied by the binding and cannot be passed as JSON.

Defaults are `stack="go"`, `Name=""` (automatic selection), `MTU=1500`, `Inet4Address=["198.18.0.1/15"]`,
`EXP_ExternalConfiguration=False`, `AutoRoute=True`, `DNSMode="disabled"`, `EXP_DisableDNSHijack=True`,
and stack `UDPTimeout="1m"`; other upstream fields keep their zero/default values. On every supported platform an omitted or empty
`Name` selects an unused `utun10`-`utun1024` name at startup, beginning at a random index. An explicit name is preserved.
`engine.device_name` reports the actual interface name after opening; selection does not reserve a name against other
processes, and a creation collision is reported by upstream. Callers may override device/network
options, including external configuration, routing and DNS settings. Device creation and MTU/link operations remain
platform-specific upstream behavior. Windows system/mixed startup uses upstream's firewall configuration.

With these defaults, upstream configures the interface addresses and TUN routes and removes its routes on close.
The application must arrange for the proxy core's outbound traffic to bypass the TUN and manage its DNS policy.
To use an externally configured device and routes, pass
`tun_options={"EXP_ExternalConfiguration": True, "AutoRoute": False}`.
Creating/configuring the device and routes requires the appropriate platform privileges.

Stack choices are `"go"` (upstream's new userspace TCP/UDP stack), `"gvisor"` (gVisor TCP/UDP),
`"system"` (OS TCP with upstream userspace UDP handling), and `"mixed"` (OS TCP plus gVisor UDP).
`stack=""` delegates selection to upstream, which selects `"go"` in this snapshot.
The Go stack's `TCPCongestionControl` option is available through `stack_options`.
This snapshot's Go parser rejects IPv6 hop-by-hop, routing, and destination option headers; gVisor remains available
for callers that need those packets. Packet processing follows the selected upstream stack.

The SOCKS handler requires an IP-literal `proxy` URL and supports optional username/password authentication. Its separate
bridge controls are `network_interface=""` (no explicit outbound SOCKS interface binding; the OS chooses the route), `log_level="error"`, `max_sessions=1024`,
`udp_timeout=60`, `connect_timeout=10`, and `tcp_idle_timeout=300` (seconds). These control forwarding connections;
upstream stack timeouts and NAT limits belong in `stack_options`. DNS packets transit through SOCKS by default.
Device DNS options configure upstream host behavior; this package does not add a sing-box DNS resolver.
SOCKS UDP requires an IP relay and unfragmented SOCKS datagrams. ICMP echo replies follow upstream's local behavior
and do not prove remote connectivity through SOCKS.

`Engine.start()` initializes synchronously; `ready` and `status` report local state; `device_name` reports the opened interface. `stop()` requests shutdown,
`wait(timeout=None)` waits for completion, and `close(timeout=5)` stops and releases the handle. Timeouts are seconds;
`wait()` returns False on timeout. Startup/runtime failures raise exceptions. Concurrent stop/wait/close are supported;
engines are one-shot and only one may be active per process. Importing or constructing an engine does not open a TUN.

On Linux/macOS, `tun_options["FileDescriptor"]` can supply a positive, borrowed descriptor. The binding duplicates it;
the caller retains the original. Zero has upstream's meaning: create/open a device. Linux's external configuration mode also
supports opening a preconfigured TUN without MTU/link mutation; GSO or network-namespace creation uses upstream's
constructor. Device privileges and actual descriptor transfer remain the application's responsibility.

Runtime provenance is available as `sing_tun.__version__`, `sing_tun.__upstream_version__`, and
`sing_tun.__upstream_commit__`. `sing_tun.capabilities()` describes the available stacks and binding behavior.

## Vendored Upstream Source

The source distribution vendors the exact sing-tun development commit recorded in `UPSTREAM_COMMIT`;
`UPSTREAM_VERSION` is `dev`. The package version identifies the development snapshot.
Files under `sing-tun-go/` preserve upstream source without modifications.
The separate `adapter/` module supplies the C ABI, Python lifecycle, SOCKS handler and descriptor ownership glue;
upstream constructors implement device and packet-stack behavior.

The GitHub Actions synchronization workflow checks stable tags hourly and supports manual dispatch. Discovery paginates
tags because upstream does not require GitHub release objects. Candidates are built/tested before source promotion; failed updates
restore the previous source and metadata. Release tags/publication require the complete wheel/sdist validation matrix.
Run the source integrity checks with:

```console
python scripts/verify_source.py
python scripts/sync-sing-tun.py verify
```

Stable releases use the same Git release tag as upstream. Development snapshots use Python versions such as
`0.9.7.dev0` and Git tags such as `v0.9.7.dev0`, with `dev` and an exact commit as upstream provenance.
Development releases are manual from `codex/development`, require the full build/test matrix, and are marked as GitHub
prereleases. The hourly stable-tag updater runs only on `main`; publishing a development snapshot does not change it.
To publish a prepared snapshot, run the distribution workflow manually on `codex/development` with the exact
commit as `checkout_ref`, `v0.9.7.dev0` as `release_tag`, and `dev` as `upstream_tag`.
Alternatively, push the matching development tag after committing the snapshot to that branch.
Developer synchronization tooling requires Python 3.12+. Publishing needs a PyPI trusted publisher for
`LorenEteval/sing-tun-python`, workflow `deploy-pypi.yml`, environment `deploy-pypi`, and the protected GitHub environment.

See [test instructions](tests/README.md) for native packet tests, synchronization tests and real-device smoke checks.

## License

This project is licensed under [GPLv3](LICENSE). Dependency license texts are included in the packaged license files.
