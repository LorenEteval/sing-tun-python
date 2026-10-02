//go:build !linux && !darwin

// SPDX-License-Identifier: GPL-3.0-or-later
package main

import tun "github.com/sagernet/sing-tun"

func openHostTun(c Config) (tun.Tun, error) { return tun.New(c.TunOptions) }
