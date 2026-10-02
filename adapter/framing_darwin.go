// SPDX-License-Identifier: GPL-3.0-or-later
package main

import "encoding/binary"

func frameIP(p []byte) []byte {
	b := make([]byte, len(p)+4)
	family := uint32(2)
	if len(p) > 0 && p[0]>>4 == 6 {
		family = 30
	}
	binary.BigEndian.PutUint32(b, family)
	copy(b[4:], p)
	return b
}
