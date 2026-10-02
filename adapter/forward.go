// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"net/url"
	"sync"
	"time"

	tun "github.com/sagernet/sing-tun"
	"github.com/sagernet/sing/common/bufio"
	"github.com/sagernet/sing/common/control"
	M "github.com/sagernet/sing/common/metadata"
	N "github.com/sagernet/sing/common/network"
	"github.com/sagernet/sing/protocol/socks"
)

type forwarder struct {
	e        *Engine
	mu       sync.Mutex
	sessions map[*session]struct{}
	closed   bool
	wg       sync.WaitGroup
}
type session struct {
	f       *forwarder
	ctx     context.Context
	cancel  context.CancelFunc
	mu      sync.Mutex
	sockets []net.Conn
	inbound io.Closer
	closed  bool
	workers sync.WaitGroup
}

func newForwarder(e *Engine) *forwarder {
	return &forwarder{e: e, sessions: make(map[*session]struct{})}
}
func (f *forwarder) count() int { f.mu.Lock(); defer f.mu.Unlock(); return len(f.sessions) }
func (f *forwarder) reserve(in io.Closer) *session {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.closed || len(f.sessions) >= f.e.cfg.MaxSessions {
		in.Close()
		return nil
	}
	ctx, cancel := context.WithCancel(f.e.ctx)
	s := &session{f: f, ctx: ctx, cancel: cancel, inbound: in}
	f.sessions[s] = struct{}{}
	f.wg.Add(1)
	return s
}
func (f *forwarder) stop() {
	f.mu.Lock()
	f.closed = true
	sessions := make([]*session, 0, len(f.sessions))
	for s := range f.sessions {
		sessions = append(sessions, s)
	}
	f.mu.Unlock()
	for _, s := range sessions {
		s.close()
	}
}
func (f *forwarder) wait() { f.wg.Wait() }
func (s *session) add(c net.Conn) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed {
		c.Close()
		return false
	}
	s.sockets = append(s.sockets, c)
	return true
}
func (s *session) close() {
	s.cancel()
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return
	}
	s.closed = true
	sockets := append([]net.Conn(nil), s.sockets...)
	s.mu.Unlock()
	s.inbound.Close()
	for _, c := range sockets {
		c.Close()
	}
}
func (s *session) finish() {
	s.close()
	s.workers.Wait()
	s.f.mu.Lock()
	delete(s.f.sessions, s)
	s.f.mu.Unlock()
	s.f.wg.Done()
}
func (s *session) client() *socks.Client {
	u, _ := url.Parse(s.f.e.cfg.Proxy)
	username, password := "", ""
	if u.User != nil {
		username = u.User.Username()
		password, _ = u.User.Password()
	}
	return socks.NewClient(&sessionDialer{s}, M.ParseSocksaddr(u.Host), socks.Version5, username, password)
}
func (s *session) handshakeDone() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, c := range s.sockets {
		c.SetDeadline(time.Time{})
	}
}

type sessionDialer struct{ s *session }

func (d *sessionDialer) DialContext(ctx context.Context, network string, dest M.Socksaddr) (net.Conn, error) {
	if !dest.Addr.IsValid() {
		return nil, errors.New("SOCKS relay must provide an IP endpoint")
	}
	if network == "udp" && dest.Addr.IsUnspecified() {
		u, _ := url.Parse(d.s.f.e.cfg.Proxy)
		dest.Addr = M.ParseSocksaddr(u.Host).Addr
	}
	dialer := net.Dialer{Timeout: seconds(d.s.f.e.cfg.ConnectTimeout)}
	if name := d.s.f.e.cfg.NetworkInterface; name != "" {
		finder := control.NewDefaultInterfaceFinder()
		if err := finder.Update(); err != nil {
			return nil, err
		}
		dialer.Control = control.BindToInterface(finder, name, -1)
	}
	c, err := dialer.DialContext(ctx, network, dest.String())
	if err != nil {
		return nil, err
	}
	if network == "udp" {
		c = &validatedUDP{c}
	}
	if !d.s.add(c) {
		return nil, context.Canceled
	}
	c.SetDeadline(time.Now().Add(seconds(d.s.f.e.cfg.ConnectTimeout)))
	// Handshakes in sing are synchronous reads; cancel actively closes their socket.
	d.s.workers.Add(1)
	go func() { defer d.s.workers.Done(); <-d.s.ctx.Done(); c.Close() }()
	return c, nil
}
func (d *sessionDialer) ListenPacket(context.Context, M.Socksaddr) (net.PacketConn, error) {
	return nil, errors.New("unexpected direct packet listen")
}

func (f *forwarder) JudgeFlow(network uint8, source, destination netip.AddrPort, first []byte) tun.FlowVerdict {
	// Let upstream apply its normal packet handling, including local ICMP echo.
	return tun.FlowVerdict{Action: tun.ActionAccept}
}

// DNSModeDisabled prevents callback dispatch: port 53 travels through UDP/TCP
// handlers unchanged. An unexpected callback is an engine error, never success.
func (f *forwarder) NewDNSPacket([]byte, M.Socksaddr, M.Socksaddr, N.PacketWriter) {
	f.e.fail("unexpected DNS hijack callback")
	f.e.Stop()
}

func (f *forwarder) NewConnectionEx(_ context.Context, in net.Conn, source, dest M.Socksaddr, onClose N.CloseHandlerFunc) {
	s := f.reserve(in)
	if s == nil {
		if onClose != nil {
			onClose(errors.New("session limit or stopping"))
		}
		return
	}
	go func() {
		var err error
		defer func() {
			s.close()
			s.workers.Wait()
			if onClose != nil {
				onClose(err)
			}
			s.finish()
		}()
		out, dialErr := s.client().DialContext(s.ctx, "tcp", dest)
		if dialErr != nil {
			err = errors.New("SOCKS TCP connect failed")
			return
		}
		if !s.add(out) {
			err = context.Canceled
			return
		}
		s.handshakeDone()
		err = bufio.CopyConn(s.ctx, &idleConn{Conn: in, timeout: seconds(f.e.cfg.TCPIdleTimeout)}, &idleConn{Conn: out, timeout: seconds(f.e.cfg.TCPIdleTimeout)})
	}()
}
func (f *forwarder) NewPacketConnectionEx(_ context.Context, in N.PacketConn, source, dest M.Socksaddr, onClose N.CloseHandlerFunc) {
	s := f.reserve(in)
	if s == nil {
		if onClose != nil {
			onClose(errors.New("session limit or stopping"))
		}
		return
	}
	go func() {
		var err error
		defer func() {
			s.close()
			s.workers.Wait()
			if onClose != nil {
				onClose(err)
			}
			s.finish()
		}()
		out, dialErr := s.client().DialContext(s.ctx, "udp", M.Socksaddr{Addr: netip.IPv4Unspecified()})
		if dialErr != nil {
			err = errors.New("SOCKS UDP association failed")
			return
		}
		if !s.add(out) {
			err = context.Canceled
			return
		}
		s.handshakeDone()
		// sing's associate connection retains the TCP control socket but does not
		// observe its EOF. Observe it here so the association cannot outlive control.
		s.mu.Lock()
		controlConn := s.sockets[0]
		s.mu.Unlock()
		s.workers.Add(1)
		go func() { defer s.workers.Done(); io.Copy(io.Discard, controlConn); s.close() }()
		packetOut := out.(*socks.AssociatePacketConn)
		err = bufio.CopyPacketConn(s.ctx, &idlePacket{PacketConn: in, timeout: seconds(f.e.cfg.UDPTimeout)}, &idlePacket{PacketConn: packetOut, timeout: seconds(f.e.cfg.UDPTimeout)})
	}()
}

// Wrap Read/Write to impose idle deadlines without a per-session polling timer.
type idleConn struct {
	net.Conn
	timeout time.Duration
}

func (c *idleConn) Read(p []byte) (int, error) {
	c.Conn.SetReadDeadline(time.Now().Add(c.timeout))
	return c.Conn.Read(p)
}
func (c *idleConn) Write(p []byte) (int, error) {
	c.Conn.SetWriteDeadline(time.Now().Add(c.timeout))
	return c.Conn.Write(p)
}
func (c *idleConn) CloseWrite() error {
	if w, ok := c.Conn.(interface{ CloseWrite() error }); ok {
		return w.CloseWrite()
	}
	return c.Close()
}

// ReadPacket/WritePacket keep every datagram's destination, including DNS.
type idlePacket struct {
	N.PacketConn
	timeout time.Duration
}

var _ tun.Handler = (*forwarder)(nil)
