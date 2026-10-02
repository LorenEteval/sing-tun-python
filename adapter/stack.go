// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"errors"
	"net"

	tun "github.com/sagernet/sing-tun"
)

// Stack construction and packet policy belong to upstream. This wrapper only
// closes Mixed's system listeners when upstream Start fails partway.
func managedStack(name string, options tun.StackOptions) (tun.Stack, error) {
	st, err := tun.NewStack(name, options)
	if err != nil {
		return nil, err
	}
	return &nativeStack{st}, nil
}

type nativeStack struct{ tun.Stack }

func (s *nativeStack) Close() error {
	if mixed, ok := s.Stack.(*tun.Mixed); ok {
		if err := mixed.System.Close(); err != nil && !errors.Is(err, net.ErrClosed) {
			return err
		}
	}
	err := s.Stack.Close()
	if errors.Is(err, net.ErrClosed) {
		return nil
	}
	return err
}
