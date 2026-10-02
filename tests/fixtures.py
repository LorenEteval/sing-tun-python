"""Owned loopback SOCKS/echo servers and raw packet helpers; no host TUN."""

import contextlib
import ipaddress
import socket
import struct
import threading
import time


def exact(sock, n):
    result = b""
    while len(result) < n:
        data = sock.recv(n - len(result))
        if not data:
            raise EOFError
        result += data
    return result


def address(host, port):
    ip = ipaddress.ip_address(host)
    return bytes([1 if ip.version == 4 else 4]) + ip.packed + struct.pack("!H", port)


def read_address(sock):
    kind = exact(sock, 1)[0]
    if kind not in (1, 4):
        raise ValueError("fixture accepts IP literals only")
    host = str(ipaddress.ip_address(exact(sock, 4 if kind == 1 else 16)))
    port = struct.unpack("!H", exact(sock, 2))[0]
    return host, port


class SocksFixture:
    """Map test wire destinations to owned loopback echo endpoints."""

    def __init__(self, auth=None, stall=False, fragmented=False, handshake_delay=0):
        self.auth, self.stall = auth, stall
        self.fragmented = fragmented
        self.handshake_delay = handshake_delay
        self.done = threading.Event()
        self.udp_response_sent = threading.Event()
        self.lock = threading.Lock()
        self.sockets, self.threads, self.controls = [], [], []
        self.seen, self.failures = [], []
        self.listener = self.socket(socket.SOCK_STREAM)
        self.listener.listen()
        self.endpoint = self.listener.getsockname()
        self.tcp_echo = self.socket(socket.SOCK_STREAM)
        self.tcp_echo.listen()
        self.udp_echo = self.socket(socket.SOCK_DGRAM)
        self.spawn(self.accept, self.listener, self.socks)
        self.spawn(self.accept, self.tcp_echo, self.echo_tcp)
        self.spawn(self.echo_udp)

    def socket(self, kind):
        sock = socket.socket(socket.AF_INET, kind)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.1)
        with self.lock:
            self.sockets.append(sock)
        return sock

    def spawn(self, target, *args):
        def checked():
            try:
                target(*args)
            except (OSError, EOFError):
                pass
            except Exception as error:
                with self.lock:
                    self.failures.append(error)

        thread = threading.Thread(target=checked, daemon=True)
        with self.lock:
            self.threads.append(thread)
        thread.start()

    def accept(self, listener, handler):
        while not self.done.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            conn.settimeout(3)
            with self.lock:
                self.sockets.append(conn)
            self.spawn(handler, conn)

    def echo_tcp(self, conn):
        with conn:
            while not self.done.is_set():
                data = conn.recv(65535)
                if not data:
                    return
                conn.sendall(data)

    def echo_udp(self):
        while not self.done.is_set():
            try:
                data, peer = self.udp_echo.recvfrom(65535)
            except socket.timeout:
                continue
            self.udp_echo.sendto(data, peer)

    def socks(self, conn):
        with conn:
            if self.done.wait(self.handshake_delay):
                return
            version, count = exact(conn, 2)
            assert version == 5
            methods = exact(conn, count)
            if self.stall:
                self.done.wait(5)
                return
            method = 2 if self.auth else 0
            assert method in methods
            conn.sendall(bytes([5, method]))
            if self.auth:
                assert exact(conn, 1) == b"\x01"
                username = exact(conn, exact(conn, 1)[0])
                password = exact(conn, exact(conn, 1)[0])
                ok = (username, password) == self.auth
                conn.sendall(bytes([1, 0 if ok else 1]))
                if not ok:
                    return
            version, command, reserved = exact(conn, 3)
            assert (version, reserved) == (5, 0)
            destination = read_address(conn)
            with self.lock:
                self.controls.append(conn)
            if command == 1:
                with self.lock:
                    self.seen.append(("tcp", destination, None))
                upstream = socket.create_connection(
                    self.tcp_echo.getsockname(), timeout=3
                )
                with self.lock:
                    self.sockets.append(upstream)
                conn.sendall(b"\x05\x00\x00" + address("127.0.0.1", 1))

                def reverse():
                    while not self.done.is_set():
                        data = upstream.recv(65535)
                        if not data:
                            return
                        conn.sendall(data)

                self.spawn(reverse)
                with upstream:
                    while not self.done.is_set():
                        data = conn.recv(65535)
                        if not data:
                            return
                        upstream.sendall(data)
            else:
                assert command == 3
                relay = self.socket(socket.SOCK_DGRAM)
                conn.sendall(b"\x05\x00\x00" + address(*relay.getsockname()))
                self.spawn(self.relay, relay)
                try:
                    while not self.done.is_set() and conn.recv(1):
                        pass
                finally:
                    relay.close()

    def relay(self, relay):
        echo = self.socket(socket.SOCK_DGRAM)
        echo.settimeout(3)
        with echo:
            while not self.done.is_set():
                try:
                    data, peer = relay.recvfrom(65535)
                except socket.timeout:
                    continue
                assert data[:3] == b"\0\0\0"
                size = 4 if data[3] == 1 else 16
                end = 4 + size + 2
                host = str(ipaddress.ip_address(data[4 : 4 + size]))
                port = struct.unpack("!H", data[4 + size : end])[0]
                payload = data[end:]
                with self.lock:
                    self.seen.append(("udp", (host, port), payload))
                echo.sendto(payload, self.udp_echo.getsockname())
                response, _ = echo.recvfrom(65535)
                header = data[:end]
                if self.fragmented:
                    header = b"\0\0\x01" + header[3:]
                relay.sendto(header + response, peer)
                self.udp_response_sent.set()

    def disconnect_controls(self):
        with self.lock:
            controls = list(self.controls)
        for sock in controls:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()

    def close(self):
        self.done.set()
        with self.lock:
            sockets = list(self.sockets)
        for sock in sockets:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        deadline = time.monotonic() + 5
        while True:
            with self.lock:
                threads = list(self.threads)
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))
            with self.lock:
                if threads == self.threads:
                    break
        assert not any(thread.is_alive() for thread in threads), "fixture thread leaked"
        assert not self.failures, repr(self.failures)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    value = sum(struct.unpack("!%dH" % (len(data) // 2), data))
    while value >> 16:
        value = (value & 65535) + (value >> 16)
    return ~value & 65535


def packet(source, destination, protocol, transport):
    src, dst = (
        ipaddress.ip_address(source).packed,
        ipaddress.ip_address(destination).packed,
    )
    if len(src) == 4:
        header = struct.pack(
            "!BBHHHBBH4s4s",
            0x45,
            0,
            20 + len(transport),
            0,
            0,
            64,
            protocol,
            0,
            src,
            dst,
        )
        header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    else:
        header = struct.pack(
            "!IHBB16s16s", 6 << 28, len(transport), protocol, 64, src, dst
        )
    return header + transport


def pseudo(source, destination, protocol, n):
    src, dst = (
        ipaddress.ip_address(source).packed,
        ipaddress.ip_address(destination).packed,
    )
    return (
        src
        + dst
        + (
            struct.pack("!BBH", 0, protocol, n)
            if len(src) == 4
            else struct.pack("!I3xB", n, protocol)
        )
    )


def udp(source, destination, sport, dport, payload):
    data = struct.pack("!HHHH", sport, dport, 8 + len(payload), 0) + payload
    value = checksum(pseudo(source, destination, 17, len(data)) + data) or 65535
    return packet(
        source, destination, 17, data[:6] + struct.pack("!H", value) + payload
    )


def icmp_echo(source, destination, identifier, sequence, payload):
    v6 = ipaddress.ip_address(source).version == 6
    protocol = 58 if v6 else 1
    data = struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, identifier, sequence) + payload
    value = checksum(
        (pseudo(source, destination, protocol, len(data)) if v6 else b"") + data
    )
    return packet(
        source, destination, protocol, data[:2] + struct.pack("!H", value) + data[4:]
    )


def ipv4_fragments(raw, split=16):
    assert raw[0] == 0x45 and split % 8 == 0 and len(raw) > 20 + split
    result = []
    for offset, data in ((0, raw[20 : 20 + split]), (split, raw[20 + split :])):
        header = bytearray(raw[:20])
        struct.pack_into(
            "!HHH",
            header,
            2,
            20 + len(data),
            0x1234,
            (0x2000 if offset == 0 else 0) | (offset // 8),
        )
        header[10:12] = b"\0\0"
        struct.pack_into("!H", header, 10, checksum(header))
        result.append(bytes(header) + data)
    return result


def tcp(source, destination, sport, dport, seq, ack, flags, payload=b""):
    data = (
        struct.pack("!HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 65535, 0, 0)
        + payload
    )
    data = (
        data[:16]
        + struct.pack("!H", checksum(pseudo(source, destination, 6, len(data)) + data))
        + data[18:]
    )
    return packet(source, destination, 6, data)


def unpack(packet):
    if packet[0] >> 4 == 4:
        index = (packet[0] & 15) * 4
        src, dst = socket.inet_ntop(socket.AF_INET, packet[12:16]), socket.inet_ntop(
            socket.AF_INET, packet[16:20]
        )
        proto = packet[9]
    else:
        index = 40
        src, dst = socket.inet_ntop(socket.AF_INET6, packet[8:24]), socket.inet_ntop(
            socket.AF_INET6, packet[24:40]
        )
        proto = packet[6]
    transport = packet[index:]
    if proto == 17:
        return src, dst, struct.unpack("!HH", transport[:4]), transport[8:]
    sport, dport, seq, ack, offset, flags = struct.unpack("!HHIIBB", transport[:14])
    return src, dst, (sport, dport, seq, ack, flags), transport[(offset >> 4) * 4 :]
