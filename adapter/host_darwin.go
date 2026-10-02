// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	tun "github.com/sagernet/sing-tun"
	"golang.org/x/sys/unix"
)

// Duplicate a borrowed descriptor; upstream owns the duplicate after success.
func openHostTun(c Config) (tun.Tun, error) {
	options := c.TunOptions
	if options.FileDescriptor == 0 {
		return tun.New(options)
	}
	fd, err := unix.FcntlInt(uintptr(options.FileDescriptor), unix.F_DUPFD_CLOEXEC, 3)
	if err != nil {
		return nil, err
	}
	options.FileDescriptor = fd
	device, err := tun.New(options)
	if err != nil {
		unix.Close(fd)
	}
	return device, err
}
