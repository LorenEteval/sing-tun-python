// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"github.com/sagernet/sing/common/buf"
	M "github.com/sagernet/sing/common/metadata"
	N "github.com/sagernet/sing/common/network"
	"net"
	"time"
)

// The pinned SOCKS helper parses past RSV/FRAG without checking them. Reject
// fragments and domain return addresses at the raw connected relay boundary.
type validatedUDP struct{ net.Conn }

func (c *validatedUDP) Read(p []byte) (int, error) {
	for {
		n, err := c.Conn.Read(p)
		if err != nil {
			return n, err
		}
		if n >= 4 && p[0] == 0 && p[1] == 0 && p[2] == 0 && (p[3] == 1 || p[3] == 4) {
			return n, nil
		}
	}
}

// CopyPacket sizes pooled buffers from the writer's headroom interfaces. Do
// not hide them behind deadline wrappers or SOCKS cannot prepend its header.
func (c *idlePacket) FrontHeadroom() int {
	return N.CalculateFrontHeadroom(c.PacketConn)
}
func (c *idlePacket) RearHeadroom() int {
	return N.CalculateRearHeadroom(c.PacketConn)
}

func (c *idlePacket) ReadPacket(b *buf.Buffer) (M.Socksaddr, error) {
	c.PacketConn.SetReadDeadline(time.Now().Add(c.timeout))
	return c.PacketConn.ReadPacket(b)
}
func (c *idlePacket) WritePacket(b *buf.Buffer, d M.Socksaddr) error {
	c.PacketConn.SetWriteDeadline(time.Now().Add(c.timeout))
	return c.PacketConn.WritePacket(b, d)
}
