// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"errors"
	tun "github.com/sagernet/sing-tun"
	"golang.org/x/sys/unix"
)

// Upstream's descriptor path skips configure/LinkSetMTU/LinkSetUp. Duplicate a
// supplied descriptor; the caller always retains its original (borrow semantics).
func openHostTun(c Config) (tun.Tun, error) {
	o := c.TunOptions
	if o.FileDescriptor == 0 && (!o.EXP_ExternalConfiguration || o.GSO || o.NetNs != "") {
		return tun.New(o)
	}
	var fd int
	var err error
	if o.FileDescriptor != 0 {
		fd, err = unix.FcntlInt(uintptr(o.FileDescriptor), unix.F_DUPFD_CLOEXEC, 3)
	} else {
		fd, err = unix.Open("/dev/net/tun", unix.O_RDWR|unix.O_CLOEXEC|unix.O_NONBLOCK, 0)
		if err == nil {
			var ifr *unix.Ifreq
			ifr, err = unix.NewIfreq(o.Name)
			if err == nil {
				ifr.SetUint16(unix.IFF_TUN | unix.IFF_NO_PI)
				err = unix.IoctlIfreq(fd, unix.TUNSETIFF, ifr)
			}
			if err != nil {
				unix.Close(fd)
			}
		}
	}
	if err != nil {
		return nil, err
	}
	if o.FileDescriptor != 0 {
		ifr, checkErr := unix.NewIfreq(o.Name)
		if checkErr == nil {
			checkErr = unix.IoctlIfreq(fd, unix.TUNGETIFF, ifr)
		}
		if checkErr != nil || ifr.Uint16()&unix.IFF_TUN == 0 || ifr.Uint16()&unix.IFF_NO_PI == 0 || (ifr.Uint16()&unix.IFF_VNET_HDR != 0) != o.GSO {
			unix.Close(fd)
			return nil, errors.New("descriptor must be an IFF_TUN|IFF_NO_PI device with matching GSO configuration")
		}
	}
	if err = unix.SetNonblock(fd, true); err != nil {
		unix.Close(fd)
		return nil, err
	}
	o.FileDescriptor = fd
	t, err := tun.New(o)
	if err != nil {
		unix.Close(fd)
	}
	return t, err
}
