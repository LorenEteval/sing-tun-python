# Test boundaries

Normal tests never create a real TUN, alter routes/DNS/firewall/proxy settings,
elevate or signal unrelated processes. Fixtures own sockets and join threads;
packet tests exercise the real Go stack, C ABI and pybind11 extension through a
private bounded memory device.

```sh
go test -C adapter -race -tags with_gvisor -mod=readonly ./...
python -m unittest discover -s tests -v
```

Install/build first (`pip install .`, or developer `setup.py build_ext --inplace`).
Set `SING_TUN_REQUIRE_NATIVE=1` to fail on missing native imports instead of skipping;
every wheel CI test sets it. Run installed tests outside the checkout:

```sh
cd /a/separate/directory
SING_TUN_REQUIRE_NATIVE=1 python -m unittest discover -s /repo/tests -p test_native.py -v
python /repo/sample/sample.py --outside /repo
```

Native tests cover authenticated IPv4/IPv6 TCP, UDP destinations/DNS query bytes at
the relay, authentication rejection, control EOF, stalled handshakes, concurrent
stop/wait, idle expiry, bounded UDP sessions, SOCKS UDP fragment rejection,
upstream local IPv4/IPv6 ICMP echo, IPv4 reassembly and IPv6 extension-header transit,
unstarted close and repeated cycles. Go tests cover partial rollback, cleanup
refusal/retained slot/retry, competing starts/restart rejection, DNS configuration,
worker counts, upstream option passthrough and Unix system/mixed constructors and
loopback lifecycle. Go tests exercise upstream local IPv4/IPv6 ICMP on every
available stack (gvisor/system/mixed on Linux). Linux tests also reject an invalid
borrowed fd repeatedly, check that the original stays open and count descriptor
release. Platform devices use upstream constructors. Use the real-device smoke
checks below to exercise privileged device initialization. Sync tests use owned
Git fixtures/mocked APIs for pagination,
numeric stable ordering, annotated/cyclic/moved tags, dirty trees, bytes/path/mode/
collision checks, staged failure/promotion rollback, idempotence, upstream-aligned
versions and release guards. Windows lacks executable-bit filesystem evidence;
sdist modes are checked directly against the manifest.

`sample/sample.py` checks installed import/provenance/API without starting a TUN.
`validate_distributions.py` checks artifact contents, native architecture/tags and
pristine sdist bytes/modes.

## Opt-in real-device smoke checks

No privileged test is enabled by default. Use only disposable Windows/macOS VMs
or Linux namespaces/VMs, a snapshot and an explicitly owned interface. Do not run
Engine.start on a normal host as a unit test. Prepare matching addresses/MTU/link
state/scoped routes with host tools and record exactly created resources. Run an
owned SOCKS fixture/core and IPv4/IPv6 TCP/UDP/DNS generators; verify received
bytes rather than ping/readiness alone.

Linux smoke should test permitted persistent attachment and actual FD transfer,
with the original remaining open and duplicate closing. Windows smoke should
compare address/route/DNS/firewall state and exercise reader wakeup; system/mixed
use upstream firewall configuration and require a disposable host. macOS smoke
must account for utun's MTU ioctl and parent DNS/route restoration after cleanup.

Stop/wait/close, check child/socket/device release, then remove only exact owned
routes/addresses/device/helper resources in finally. Destroy the namespace or
restore the snapshot if cleanup fails.
