// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"io"
	"sync"

	"github.com/sagernet/gvisor/pkg/buffer"
	"github.com/sagernet/gvisor/pkg/tcpip"
	"github.com/sagernet/gvisor/pkg/tcpip/header"
	"github.com/sagernet/gvisor/pkg/tcpip/link/channel"
	"github.com/sagernet/gvisor/pkg/tcpip/stack"
	tun "github.com/sagernet/sing-tun"
	"github.com/sagernet/sing/common/buf"
)

// Private in-memory test endpoint; never wraps a production TUN.
type packetDevice struct {
	raw     tun.Tun
	e       *Engine
	ep      *channel.Endpoint
	wg      sync.WaitGroup
	writeMu sync.Mutex
}

func newPacketDevice(e *Engine, raw tun.Tun) *packetDevice { return &packetDevice{e: e, raw: raw} }
func (d *packetDevice) Name() (string, error)              { return d.raw.Name() }
func (d *packetDevice) Start() error                       { return d.raw.Start() }
func (d *packetDevice) UpdateRouteOptions(tun.Options) error {
	return errors.New("host owns network configuration")
}
func (d *packetDevice) Close() error {
	err := d.raw.Close()
	if d.ep != nil {
		d.ep.Close()
	}
	return err
}
func (d *packetDevice) wait() { d.wg.Wait() }

func (d *packetDevice) Read(p []byte) (int, error) {
	n, err := d.raw.Read(p)
	if err != nil && d.e.ctx.Err() == nil {
		d.e.fail("TUN packet reader failed")
		d.e.Stop()
	}
	return n, err
}
func (d *packetDevice) Write(p []byte) (int, error) {
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	return d.raw.Write(p)
}
func (d *packetDevice) writeIP(p []byte) (int, error) { return d.Write(frameIP(p)) }
func (d *packetDevice) WritePacket(pkt *stack.PacketBuffer) (int, error) {
	var p []byte
	for _, s := range pkt.AsSlices() {
		p = append(p, s...)
	}
	return d.writeIP(p)
}
func (d *packetDevice) NewEndpoint() (stack.LinkEndpoint, stack.NICOptions, error) {
	d.ep = channel.New(256, d.e.cfg.TunOptions.MTU, "")
	d.wg.Add(2)
	go func() {
		defer d.wg.Done()
		p := make([]byte, int(d.e.cfg.TunOptions.MTU)+tun.PacketOffset)
		for {
			n, err := d.Read(p)
			if err != nil {
				return
			}
			if n <= tun.PacketOffset {
				continue
			}
			payload := append([]byte(nil), p[tun.PacketOffset:n]...)
			var protocol tcpip.NetworkProtocolNumber
			switch payload[0] >> 4 {
			case 4:
				protocol = header.IPv4ProtocolNumber
			case 6:
				protocol = header.IPv6ProtocolNumber
			default:
				continue
			}
			pkt := stack.NewPacketBuffer(stack.PacketBufferOptions{Payload: buffer.MakeWithData(payload), IsForwardedPacket: true})
			d.ep.InjectInbound(protocol, pkt)
			pkt.DecRef()
		}
	}()
	go func() {
		defer d.wg.Done()
		for {
			pkt := d.ep.ReadContext(d.e.ctx)
			if pkt == nil {
				return
			}
			_, err := d.WritePacket(pkt)
			pkt.DecRef()
			if err != nil && d.e.ctx.Err() == nil {
				d.e.fail("TUN packet writer failed")
				d.e.Stop()
				return
			}
		}
	}()
	return d.ep, stack.NICOptions{}, nil
}

// Explicit diagnostic boundary, never selected by public Engine/Config.
type memoryTun struct {
	input, output chan []byte
	done          chan struct{}
	once          sync.Once
	goTun         *tun.MemoryTun
}

func newMemoryTun() *memoryTun {
	return &memoryTun{input: make(chan []byte, 256), output: make(chan []byte, 256), done: make(chan struct{})}
}

// Upstream's own in-memory device exercises the new stack without host changes.
func (m *memoryTun) enableGo(mtu int) {
	m.goTun = tun.NewMemoryTun(tun.MemoryTunOptions{MTU: mtu, Outbound: func(packets []*buf.Buffer) {
		defer buf.ReleaseMulti(packets)
		for _, packet := range packets {
			select {
			case m.output <- append([]byte(nil), packet.Bytes()...):
			case <-m.done:
				return
			}
		}
	}})
}
func (m *memoryTun) Name() (string, error) { return "memory", nil }
func (m *memoryTun) Start() error          { return nil }
func (m *memoryTun) Close() error {
	m.once.Do(func() { close(m.done) })
	if m.goTun != nil {
		return m.goTun.Close()
	}
	return nil
}
func (m *memoryTun) UpdateRouteOptions(tun.Options) error { return nil }
func (m *memoryTun) Read(p []byte) (int, error) {
	select {
	case b := <-m.input:
		return copy(p, frameIP(b)), nil
	case <-m.done:
		return 0, io.EOF
	}
}
func (m *memoryTun) Write(p []byte) (int, error) {
	b := append([]byte(nil), p[tun.PacketOffset:]...)
	select {
	case m.output <- b:
		return len(p), nil
	case <-m.done:
		return 0, io.ErrClosedPipe
	}
}
func (m *memoryTun) inject(ctx context.Context, p []byte) error {
	if m.goTun != nil {
		if err := ctx.Err(); err != nil {
			return err
		}
		n, err := m.goTun.WritePackets([][]byte{p})
		if err == nil && n != 1 {
			return io.ErrShortWrite
		}
		return err
	}
	select {
	case m.input <- append([]byte(nil), p...):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	case <-m.done:
		return io.ErrClosedPipe
	}
}

var _ tun.GVisorTun = (*packetDevice)(nil)
