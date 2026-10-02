// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/netip"
	"net/url"
	"reflect"
	"runtime"
	"strconv"
	"strings"
	"time"

	tun "github.com/sagernet/sing-tun"
)

type Config struct {
	Proxy            string           `json:"proxy"`
	Stack            string           `json:"stack"`
	LogLevel         string           `json:"log_level"`
	NetworkInterface string           `json:"network_interface"`
	MaxSessions      int              `json:"max_sessions"`
	UDPTimeout       float64          `json:"udp_timeout"`
	ConnectTimeout   float64          `json:"connect_timeout"`
	TCPIdleTimeout   float64          `json:"tcp_idle_timeout"`
	TunParameters    json.RawMessage  `json:"tun_options"`
	StackParameters  json.RawMessage  `json:"stack_options"`
	TunOptions       tun.Options      `json:"-"`
	StackOptions     tun.StackOptions `json:"-"`
}

func parseConfig(raw []byte) (Config, error) {
	c := Config{Stack: "gvisor", LogLevel: "error", MaxSessions: 1024, UDPTimeout: 60, ConnectTimeout: 10, TCPIdleTimeout: 300}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.DisallowUnknownFields()
	if err := d.Decode(&c); err != nil {
		return c, fmt.Errorf("invalid configuration JSON")
	}
	if err := d.Decode(new(any)); err != io.EOF {
		return c, fmt.Errorf("invalid trailing configuration")
	}
	c.TunOptions = tun.Options{MTU: 1500, Inet4Address: []netip.Prefix{netip.MustParsePrefix("198.18.0.1/15")}, DNSMode: tun.DNSModeDisabled, EXP_DisableDNSHijack: true, EXP_ExternalConfiguration: true}
	c.StackOptions = tun.StackOptions{UDPTimeout: time.Minute}
	if err := decodeOptions(c.TunParameters, &c.TunOptions); err != nil {
		return c, fmt.Errorf("tun_options: %w", err)
	}
	if err := decodeOptions(c.StackParameters, &c.StackOptions); err != nil {
		return c, fmt.Errorf("stack_options: %w", err)
	}
	if c.TunOptions.MTU < 68 || c.TunOptions.MTU > 65535 {
		return c, fmt.Errorf("MTU must be between 68 and 65535")
	}
	if c.TunOptions.FileDescriptor < 0 || (runtime.GOOS == "windows" && c.TunOptions.FileDescriptor != 0) {
		return c, fmt.Errorf("FileDescriptor requires a nonnegative Unix descriptor")
	}
	if c.StackOptions.UDPTimeout <= 0 || c.StackOptions.ICMPTimeout < 0 {
		return c, fmt.Errorf("invalid stack timeout")
	}
	if c.StackOptions.UDPMapping > tun.NATMappingAddressAndPortDependent || c.StackOptions.UDPFiltering > tun.NATFilteringAddressAndPortDependent {
		return c, fmt.Errorf("invalid UDP NAT mode")
	}
	if c.MaxSessions < 1 || c.MaxSessions > 16384 {
		return c, fmt.Errorf("max_sessions must be between 1 and 16384")
	}
	for _, v := range []float64{c.UDPTimeout, c.ConnectTimeout, c.TCPIdleTimeout} {
		if !(v >= 0.05 && v <= 86400) {
			return c, fmt.Errorf("SOCKS timeouts must be between 0.05 and 86400 seconds")
		}
	}
	switch c.LogLevel {
	case "trace", "debug", "info", "warn", "error", "silent":
	default:
		return c, fmt.Errorf("invalid log level")
	}
	if !availableStack(c.Stack) {
		return c, fmt.Errorf("stack unavailable on this platform")
	}
	if strings.ContainsAny(c.NetworkInterface, "\x00\r\n") {
		return c, fmt.Errorf("invalid SOCKS network interface")
	}
	u, err := url.Parse(c.Proxy)
	if err != nil || u.Scheme != "socks5" || u.Path != "" || u.RawQuery != "" || u.Fragment != "" {
		return c, fmt.Errorf("proxy must be a socks5 URL with an IP endpoint")
	}
	host, port, err := net.SplitHostPort(u.Host)
	if err != nil {
		return c, fmt.Errorf("proxy requires an IP address and port")
	}
	if _, err = netip.ParseAddr(host); err != nil {
		return c, fmt.Errorf("proxy endpoint must be an IP literal; no direct DNS resolution")
	}
	n, err := strconv.Atoi(port)
	if err != nil || n < 1 || n > 65535 {
		return c, fmt.Errorf("invalid proxy port")
	}
	if u.User != nil {
		pw, _ := u.User.Password()
		if len(u.User.Username()) < 1 || len(u.User.Username()) > 255 || len(pw) < 1 || len(pw) > 255 {
			return c, fmt.Errorf("SOCKS credentials must contain 1 to 255 UTF-8 bytes each")
		}
	}
	return c, nil
}

// Use the actual upstream fields; newly added JSON-compatible options need no
// binding mapping. Runtime objects remain owned by the native lifecycle glue.
func decodeOptions(raw json.RawMessage, target any) error {
	if len(raw) == 0 {
		return nil
	}
	var values map[string]json.RawMessage
	if err := json.Unmarshal(raw, &values); err != nil || values == nil {
		return fmt.Errorf("expected an options object")
	}
	typ := reflect.TypeOf(target).Elem()
	for name, value := range values {
		field, ok := typ.FieldByName(name)
		if !ok || !field.IsExported() {
			return fmt.Errorf("unknown upstream option %s", name)
		}
		if field.Type.Kind() == reflect.Interface || name == "TunOptions" {
			return fmt.Errorf("%s is a binding-owned runtime object", name)
		}
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return fmt.Errorf("%s cannot be null", name)
		}
		if field.Type == reflect.TypeOf(time.Duration(0)) && len(value) > 0 && value[0] == '"' {
			var text string
			if err := json.Unmarshal(value, &text); err != nil {
				return fmt.Errorf("invalid duration for %s", name)
			}
			duration, err := time.ParseDuration(text)
			if err != nil {
				return fmt.Errorf("invalid duration for %s", name)
			}
			values[name], _ = json.Marshal(duration)
		}
	}
	data, _ := json.Marshal(values)
	if err := json.Unmarshal(data, target); err != nil {
		return fmt.Errorf("invalid upstream option value")
	}
	return nil
}

func availableStack(s string) bool {
	return (runtime.GOOS == "windows" || runtime.GOOS == "linux" || runtime.GOOS == "darwin") && (s == "" || s == "gvisor" || s == "system" || s == "mixed")
}
func seconds(s float64) time.Duration { return time.Duration(s * float64(time.Second)) }

// Match Xray's automatic names: random starting index, then scan unused
// utun10..utun1024 once. Device creation still belongs to upstream sing-tun.
func availableTunName(interfaces []net.Interface, start int) (string, error) {
	used := make(map[string]bool, len(interfaces))
	for _, iface := range interfaces {
		used[iface.Name] = true
	}
	for offset := 0; offset < 1015; offset++ {
		name := "utun" + strconv.Itoa(10+(start-10+offset)%1015)
		if !used[name] {
			return name, nil
		}
	}
	return "", fmt.Errorf("no available TUN interface name in range utun10-utun1024")
}
