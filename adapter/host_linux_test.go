// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"golang.org/x/sys/unix"
	"os"
	"testing"
)

func TestLinuxInvalidBorrowedFDRemainsOwnedByCaller(t *testing.T) {
	file, err := os.CreateTemp(t.TempDir(), "owned-fd")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	c := testConfig(t)
	fd := int(file.Fd())
	c.TunOptions.FileDescriptor = fd
	before, err := os.ReadDir("/proc/self/fd")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 20; i++ {
		if device, err := openHostTun(c); err == nil || device != nil {
			t.Fatal("non-TUN descriptor accepted")
		}
		if _, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); err != nil {
			t.Fatal("borrowed original closed", err)
		}
	}
	after, err := os.ReadDir("/proc/self/fd")
	if err != nil || len(after) != len(before) {
		t.Fatal("partial duplicates leaked", len(before), len(after), err)
	}
}
