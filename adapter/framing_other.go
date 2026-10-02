//go:build !darwin

// SPDX-License-Identifier: GPL-3.0-or-later
package main

func frameIP(p []byte) []byte { return p }
