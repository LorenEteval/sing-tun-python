"""Exercise Go + C ABI + pybind11 with a private in-memory packet boundary."""

import concurrent.futures
import json
import ipaddress
import os
import pathlib
import sys
import struct
import time
import unittest

from fixtures import (
    SocksFixture,
    checksum,
    icmp_echo,
    ipv4_fragments,
    packet,
    pseudo,
    tcp,
    udp,
    unpack,
)

try:
    from sing_tun import Config, Engine, _native, capabilities
except ImportError:
    _native = None
    if os.environ.get("SING_TUN_REQUIRE_NATIVE") == "1":
        raise


@unittest.skipIf(_native is None, "build/install the native extension first")
class NativeTests(unittest.TestCase):
    def engine(self, fixture, **options):
        c = Config(proxy="socks5://%s:%s" % fixture.endpoint, **options)
        e = _native.Engine(c._json(), True)
        e.start()
        self.addCleanup(lambda: self.assertTrue(e.close(5000)))
        return e

    def test_validation_redacts_and_capabilities(self):
        for options in (
            {"tun_options": {"MTU": 1}},
            {"stack": "lwip"},
            {"max_sessions": 0},
            {"tun_options": {"UnknownOption": True}},
            {"udp_timeout": float("nan")},
            {"tun_options": {"MTU": True}},
        ):
            with self.subTest(options=options), self.assertRaises(
                (ValueError, TypeError)
            ):
                Config(proxy="socks5://127.0.0.1:1080", **options)
        with self.assertRaises(ValueError) as raised:
            Config(proxy="socks5://username:secret@bad.example:123")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("username", str(raised.exception))
        if sys.platform == "win32":
            self.assertEqual(capabilities()["stacks"], ("gvisor", "system", "mixed"))

    def test_upstream_option_passthrough(self):
        # Accept native routing/DNS/stack choices without starting a host device.
        original = {
            "AutoRoute": True,
            "EXP_ExternalConfiguration": False,
            "DNSMode": "native",
        }
        config = Config(
            proxy="socks5://127.0.0.1:1", stack="system", tun_options=original
        )
        original["AutoRoute"] = False
        self.assertTrue(config.tun_options["AutoRoute"])
        for options in (
            {"tun_options": {"Logger": None}},
            {"stack_options": {"UDPTimeout": "bad"}},
            {"stack_options": {"Handler": {}}},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Config(proxy="socks5://127.0.0.1:1", **options)
        with SocksFixture() as fixture:
            e = self.engine(
                fixture,
                tun_options={"MTU": 1400},
                stack_options={
                    "UDPTimeout": "200ms",
                    "ICMPTimeout": "5s",
                    "UDPNATMax": 8,
                    "UDPMapping": 2,
                },
            )
            for dest in ("198.19.0.9", "198.19.0.10"):
                e.inject(udp("198.18.0.2", dest, 45678, 53, b"native options"))
                self.assertEqual(unpack(e.receive(3000))[3], b"native options")
            self.assertEqual(json.loads(e.snapshot())["sessions"], 2)
            self.assertEqual(json.loads(e.snapshot())["device_name"], "memory")

    def test_no_start_import_and_created_close(self):
        e = Engine(Config(proxy="socks5://127.0.0.1:1"))
        self.assertFalse(e.ready)
        self.assertEqual(e.device_name, "")
        self.assertFalse(e.wait(0))
        e.close()
        e.close()
        self.assertTrue(e.wait(0))
        with self.assertRaises(RuntimeError):
            e.start()

    def test_competing_start_and_concurrent_close(self):
        with SocksFixture() as fixture:
            e = self.engine(fixture)
            other = _native.Engine(Config(proxy="socks5://127.0.0.1:1")._json(), True)
            with self.assertRaisesRegex(RuntimeError, "one active"):
                other.start()
            self.assertTrue(other.close(3000))
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                jobs = [pool.submit(e.close, 3000) for _ in range(4)]
                jobs += [pool.submit(e.wait, 3000) for _ in range(4)]
                stops = [pool.submit(e.stop) for _ in range(4)]
                self.assertTrue(all(f.result(5) for f in jobs))
                for f in stops:
                    f.result(5)
            with self.assertRaises(RuntimeError):
                e.start()

    def test_udp_dns_multiple_destinations_and_ipv6(self):
        with SocksFixture() as fixture:
            e = self.engine(
                fixture,
                tun_options={
                    "Inet4Address": ["198.18.0.1/15"],
                    "Inet6Address": ["fd00::1/64"],
                },
            )
            query = (
                bytes.fromhex("123401000001000000000000")
                + b"\x07example\x03com\0\0\x01\0\x01"
            )
            cases = [
                ("198.18.0.2", "198.19.0.9", 53, query),
                ("198.18.0.2", "198.19.0.10", 12345, b"second destination"),
                ("fd00::2", "2001:db8::9", 53, query),
            ]
            for source, dest, port, payload in cases:
                e.inject(udp(source, dest, 45678, port, payload))
                response = e.receive(3000)
                self.assertTrue(response)
                src, dst, ports, body = unpack(response)
                self.assertEqual(
                    (src, dst, ports, body), (dest, source, (port, 45678), payload)
                )
            self.assertEqual(
                [(row[1], row[2]) for row in fixture.seen],
                [((dest, port), payload) for _, dest, port, payload in cases],
            )
            e.stop()
            self.assertTrue(e.wait(3000))
            self.assertEqual(json.loads(e.snapshot())["sessions"], 0)

    def roundtrip_tcp(
        self, e, source, dest, port=443, payload=b"SOCKS TCP native boundary"
    ):
        e.inject(tcp(source, dest, 45679, port, 1000, 0, 2))
        response = e.receive(3000)
        self.assertTrue(response, "no SYN-ACK")
        _, _, fields, _ = unpack(response)
        self.assertEqual(fields[4] & 0x12, 0x12)
        remote = fields[2] + 1
        e.inject(tcp(source, dest, 45679, port, 1001, remote, 0x10))
        e.inject(tcp(source, dest, 45679, port, 1001, remote, 0x18, payload))
        deadline, received = time.monotonic() + 3, b""
        while time.monotonic() < deadline and len(received) < len(payload):
            p = e.receive(500)
            if p:
                _, _, fields, body = unpack(p)
                received += body
                if body:
                    remote = fields[2] + len(body)
                    e.inject(
                        tcp(
                            source,
                            dest,
                            45679,
                            port,
                            1001 + len(payload),
                            fields[2] + len(body),
                            0x10,
                        )
                    )
        self.assertEqual(received, payload)
        return 1001 + len(payload), remote

    def test_tcp_idle_and_half_close_release(self):
        with SocksFixture() as fixture:
            for half_close in (False, True):
                e = self.engine(fixture, tcp_idle_timeout=0.2)
                seq, ack = self.roundtrip_tcp(e, "198.18.0.2", "198.19.0.9")
                if half_close:
                    e.inject(
                        tcp("198.18.0.2", "198.19.0.9", 45679, 443, seq, ack, 0x11)
                    )
                deadline = time.monotonic() + 3
                while (
                    json.loads(e.snapshot())["sessions"] and time.monotonic() < deadline
                ):
                    p = e.receive(50)
                    if p:
                        _, _, fields, body = unpack(p)
                        if fields[4] & 1:
                            e.inject(
                                tcp(
                                    "198.18.0.2",
                                    "198.19.0.9",
                                    45679,
                                    443,
                                    seq + int(half_close),
                                    fields[2] + len(body) + 1,
                                    0x10,
                                )
                            )
                self.assertEqual(json.loads(e.snapshot())["sessions"], 0)
                self.assertTrue(e.close(3000))

    def test_tcp_authenticated_ipv4_ipv6(self):
        with SocksFixture(auth=(b"user", b"pass")) as fixture:
            c = Config(
                proxy="socks5://user:pass@%s:%s" % fixture.endpoint,
                tun_options={
                    "Inet4Address": ["198.18.0.1/15"],
                    "Inet6Address": ["fd00::1/64"],
                },
            )
            e = _native.Engine(c._json(), True)
            e.start()
            try:
                self.roundtrip_tcp(e, "198.18.0.2", "198.19.0.9")
                self.roundtrip_tcp(e, "fd00::2", "2001:db8::9")
            finally:
                self.assertTrue(e.close(5000))
            self.assertEqual(
                [row[1] for row in fixture.seen],
                [("198.19.0.9", 443), ("2001:db8::9", 443)],
            )

    def test_auth_failure_and_control_disconnect_release_sessions(self):
        with SocksFixture(auth=(b"user", b"pass")) as fixture:
            config = Config(proxy="socks5://user:wrong@%s:%s" % fixture.endpoint)
            e = _native.Engine(config._json(), True)
            e.start()
            self.addCleanup(lambda: self.assertTrue(e.close(5000)))
            e.inject(udp("198.18.0.2", "198.19.0.9", 45678, 53, b"query"))
            self.assertFalse(e.receive(300))
            self.assertFalse(fixture.seen)
            self.assertTrue(e.close(3000))
        with SocksFixture() as fixture:
            e = self.engine(fixture)
            e.inject(udp("198.18.0.2", "198.19.0.9", 45678, 53, b"query"))
            self.assertTrue(e.receive(3000))
            fixture.disconnect_controls()
            deadline = time.monotonic() + 3
            while json.loads(e.snapshot())["sessions"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(json.loads(e.snapshot())["sessions"], 0)

    def test_stop_interrupts_handshake_and_concurrent_wait(self):
        with SocksFixture(stall=True) as fixture:
            e = self.engine(fixture, connect_timeout=10)
            e.inject(udp("198.18.0.2", "198.19.0.9", 45678, 53, b"query"))
            deadline = time.monotonic() + 2
            while (
                json.loads(e.snapshot())["sessions"] == 0
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                waits = [pool.submit(e.wait, 3000) for _ in range(5)]
                stops = [pool.submit(e.stop) for _ in range(5)]
                self.assertTrue(all(f.result(5) for f in waits))
                for future in stops:
                    future.result(5)
            self.assertEqual(json.loads(e.snapshot())["sessions"], 0)

    def test_upstream_local_icmp_and_repeated_native_cycles(self):
        with SocksFixture() as fixture:
            for sequence in range(10):
                e = self.engine(fixture)
                # No SOCKS forwarding is involved in these upstream local replies.
                # gVisor retains its IPv6 behavior even with an IPv4-only prefix.
                for source, dest, v6 in (
                    ("198.18.0.2", "198.19.0.9", False),
                    ("fd00::2", "2001:db8::9", True),
                ):
                    payload = b"local echo does not prove remote reachability"
                    e.inject(icmp_echo(source, dest, 1234, sequence, payload))
                    response = e.receive(3000)
                    self.assertTrue(response)
                    body = response[40 if v6 else 20 :]
                    self.assertEqual(body[:2], bytes([129 if v6 else 0, 0]))
                    self.assertEqual(struct.unpack("!HH", body[4:8]), (1234, sequence))
                    self.assertEqual(body[8:], payload)
                    self.assertEqual(
                        response[8:24] if v6 else response[12:16],
                        ipaddress.ip_address(dest).packed,
                    )
                    self.assertEqual(
                        response[24:40] if v6 else response[16:20],
                        ipaddress.ip_address(source).packed,
                    )
                    self.assertEqual(
                        checksum(
                            (pseudo(dest, source, 58, len(body)) if v6 else b"") + body
                        ),
                        0,
                    )
                self.assertTrue(e.close(3000))
            self.assertFalse(fixture.seen)

    def test_upstream_ipv4_reassembly_and_ipv6_extension(self):
        with SocksFixture() as fixture:
            e = self.engine(fixture)
            payload = b"fragmented IPv4 UDP reaches the SOCKS relay"
            first, second = ipv4_fragments(
                udp("198.18.0.2", "198.19.0.9", 45678, 53, payload)
            )
            e.inject(first)
            self.assertFalse(e.receive(20))
            e.inject(second)
            response = e.receive(3000)
            self.assertTrue(response)
            self.assertEqual(unpack(response)[3], payload)
            payload6 = b"IPv6 hop-by-hop UDP reaches the SOCKS relay"
            raw = udp("fd00::2", "2001:db8::9", 45679, 53, payload6)
            e.inject(
                packet("fd00::2", "2001:db8::9", 0, b"\x11\0" + b"\0" * 6 + raw[40:])
            )
            response = e.receive(3000)
            self.assertTrue(response)
            self.assertEqual(unpack(response)[3], payload6)
            self.assertEqual([item[2] for item in fixture.seen], [payload, payload6])

    def test_udp_bound_idle_timeout_and_fragment_rejection(self):
        with SocksFixture() as fixture:
            e = self.engine(fixture, max_sessions=2, udp_timeout=0.1)
            for port in (41001, 41002):
                e.inject(udp("198.18.0.2", "198.19.0.9", port, 53, b"query"))
                self.assertTrue(e.receive(3000))
            for port in range(41003, 41030):
                e.inject(udp("198.18.0.2", "198.19.0.9", port, 53, b"excess"))
            self.assertLessEqual(json.loads(e.snapshot())["sessions"], 2)
            deadline = time.monotonic() + 3
            while json.loads(e.snapshot())["sessions"] and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(json.loads(e.snapshot())["sessions"], 0)
            self.assertTrue(e.close(3000))
        with SocksFixture(fragmented=True) as fixture:
            e = self.engine(fixture, udp_timeout=0.1)
            e.inject(udp("198.18.0.2", "198.19.0.9", 45678, 53, b"query"))
            self.assertFalse(e.receive(300))
            self.assertTrue(fixture.seen)


if __name__ == "__main__":
    unittest.main()
