// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"bytes"
	"context"
	"encoding/hex"
	"errors"
	tun "github.com/sagernet/sing-tun"
	M "github.com/sagernet/sing/common/metadata"
	"net"
	"net/netip"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"
)

func testConfig(t *testing.T) Config {
	t.Helper()
	c, err := parseConfig([]byte(`{"proxy":"socks5://127.0.0.1:1080"}`))
	if err != nil {
		t.Fatal(err)
	}
	return c
}
func finishEngine(t *testing.T, e *Engine) {
	t.Helper()
	e.Stop()
	if !e.Wait(3 * time.Second) {
		t.Fatalf("cleanup did not finish: %+v", e.snapshot())
	}
	if e.snapshot().Sessions != 0 {
		t.Fatal("sessions retained")
	}
}
func TestRepeatedLifecycleConcurrentStopWait(t *testing.T) {
	baseline := runtime.NumGoroutine()
	for i := 0; i < 20; i++ {
		e := newEngine(testConfig(t), true)
		if err := e.Start(); err != nil {
			t.Fatal(err)
		}
		if e.snapshot().State != "ready" || e.snapshot().DeviceName != "memory" {
			t.Fatal(e.snapshot())
		}
		other := newEngine(testConfig(t), true)
		if other.Start() == nil {
			t.Fatal("concurrent start accepted")
		}
		other.Stop()
		if !other.Wait(time.Second) {
			t.Fatal("unstarted engine cleanup")
		}
		var wg sync.WaitGroup
		for j := 0; j < 10; j++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				e.Stop()
				if !e.Wait(3 * time.Second) {
					t.Error("wait timed out")
				}
			}()
		}
		wg.Wait()
		finishEngine(t, e)
		if e.Start() == nil {
			t.Fatal("restart accepted")
		}
	}
	deadline := time.Now().Add(time.Second)
	for runtime.NumGoroutine() > baseline && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if n := runtime.NumGoroutine(); n > baseline {
		t.Fatalf("goroutines retained: baseline %d, final %d", baseline, n)
	}
}

type failingStack struct {
	closeCount int
	fail       bool
}

func (s *failingStack) Start() error  { return errors.New("injected partial start") }
func (s *failingStack) ResetNetwork() {}
func (s *failingStack) Close() error {
	s.closeCount++
	if s.fail {
		return errors.New("refuses cleanup")
	}
	return nil
}
func TestPartialStartRollback(t *testing.T) {
	e := newEngine(testConfig(t), true)
	s := &failingStack{}
	e.makeStack = func(string, tun.StackOptions) (tun.Stack, error) { return s, nil }
	if e.Start() == nil {
		t.Fatal("failure swallowed")
	}
	finishEngine(t, e)
	select {
	case <-e.fake.done:
	default:
		t.Fatal("device not closed")
	}
	if s.closeCount != 1 {
		t.Fatal("stack not released")
	}
	if e.snapshot().State != "failed" {
		t.Fatal(e.snapshot())
	}
}
func TestCleanupRefusalRetainsSlot(t *testing.T) {
	e := newEngine(testConfig(t), true)
	s := &failingStack{fail: true}
	e.makeStack = func(string, tun.StackOptions) (tun.Stack, error) { return s, nil }
	e.Start()
	if e.Wait(30 * time.Millisecond) {
		t.Fatal("failed cleanup falsely terminal")
	}
	deadline := time.Now().Add(time.Second)
	for e.snapshot().State != "cleanup_failed" && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	other := newEngine(testConfig(t), true)
	if other.Start() == nil {
		t.Fatal("retained slot released")
	}
	other.Stop()
	other.Wait(time.Second)
	e.initMu.Lock()
	s.fail = false
	e.initMu.Unlock()
	finishEngine(t, e)
}
func TestConfigurationAndDNSPolicy(t *testing.T) {
	for _, raw := range []string{`{"proxy":"socks5://secret:password@invalid:1"}`, `{"proxy":"socks5://127.0.0.1:1","tun_options":{"MTU":1}}`, `{"proxy":"socks5://127.0.0.1:1","max_sessions":0}`, `{"proxy":"socks5://127.0.0.1:1","stack":"lwip"}`, `{"proxy":"socks5://127.0.0.1:1","unknown":true}`} {
		if _, err := parseConfig([]byte(raw)); err == nil {
			t.Fatal("invalid config accepted")
		}
	}
	c := testConfig(t)
	o := c.TunOptions
	if o.AutoRoute || o.DNSMode != tun.DNSModeDisabled || !o.EXP_DisableDNSHijack || !o.EXP_ExternalConfiguration || len(o.Inet4Address) != 1 {
		t.Fatal("host/DNS/prefix policy")
	}
}
func TestUnavailableAndSystemPrefix(t *testing.T) {
	if runtime.GOOS == "windows" {
		if !availableStack("system") || !availableStack("mixed") {
			t.Fatal("upstream stack unavailable")
		}
	}
	if runtime.GOOS != "windows" {
		for _, name := range []string{"system", "mixed"} {
			c := testConfig(t)
			c.Stack = name
			o := c.TunOptions
			e := newEngine(c, true)
			st, err := tun.NewStack(name, tun.StackOptions{Context: e.ctx, Tun: newPacketDevice(e, e.fake), TunOptions: o, UDPTimeout: time.Second})
			if err != nil || st == nil {
				t.Fatal("stack constructor unavailable", name, err)
			}
			e.cancel()
			if mixed, ok := st.(*tun.Mixed); ok {
				mixed.System.Close()
			}
			st.Close()
		}
	}
}

func TestLegacyStacksLoopbackLifecycle(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("system/mixed would mutate firewall")
	}
	for _, name := range []string{"system", "mixed"} {
		c := testConfig(t)
		c.Stack = name
		// Test-only prefix: listen on an existing loopback address without TUN
		// or host configuration; no real device is opened.
		c.TunOptions.Inet4Address = []netip.Prefix{netip.MustParsePrefix("127.0.0.1/8")}
		e := newEngine(c, false)
		raw := newMemoryTun()
		e.openDevice = func(Config) (tun.Tun, error) { return newPacketDevice(e, raw), nil }
		baseline := runtime.NumGoroutine()
		if err := e.Start(); err != nil {
			t.Fatal(name, err)
		}
		finishEngine(t, e)
		deadline := time.Now().Add(time.Second)
		for runtime.NumGoroutine() > baseline && time.Now().Before(deadline) {
			runtime.Gosched()
		}
		if n := runtime.NumGoroutine(); n > baseline {
			t.Fatalf("%s retained workers: %d -> %d", name, baseline, n)
		}
	}
}

func TestUpstreamLocalICMPEcho(t *testing.T) {
	for _, name := range []string{"gvisor", "system", "mixed"} {
		if !availableStack(name) || (runtime.GOOS == "windows" && name != "gvisor") {
			continue
		}
		t.Run(name, func(t *testing.T) {
			c := testConfig(t)
			c.Stack = name
			// Only system/mixed listeners use this existing loopback address.
			// Packet input/output remains in memory; no OS TUN or host setup.
			c.TunOptions.Inet4Address = []netip.Prefix{netip.MustParsePrefix("127.0.0.1/8")}
			e := newEngine(c, false)
			raw := newMemoryTun()
			e.openDevice = func(Config) (tun.Tun, error) { return newPacketDevice(e, raw), nil }
			defer finishEngine(t, e)
			if err := e.Start(); err != nil {
				t.Fatal(err)
			}
			for _, vector := range []struct {
				packet string
				offset int
				reply  byte
			}{
				{"4500002f00000000400178b0c6120002cb0071090800526b04d20001757073747265616d206c6f63616c206563686f", 20, 0},
				{"60000000001b3a40fd00000000000000000000000000000220010db80000000000000000000000098000af5004d20001757073747265616d206c6f63616c206563686f", 40, 129},
			} {
				request, err := hex.DecodeString(vector.packet)
				if err != nil {
					t.Fatal(err)
				}
				if err = raw.inject(e.ctx, request); err != nil {
					t.Fatal(err)
				}
				select {
				case reply := <-raw.output:
					if len(reply) != len(request) || reply[vector.offset] != vector.reply || !bytes.Equal(reply[vector.offset+4:], request[vector.offset+4:]) {
						t.Fatalf("unexpected local ICMP reply: %x", reply)
					}
				case <-time.After(time.Second):
					t.Fatal("upstream local echo reply missing")
				}
			}
			if e.snapshot().Sessions != 0 {
				t.Fatal("local ICMP created a SOCKS session")
			}
		})
	}
}

func TestCloseWaitsForCallback(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	serverDone := make(chan struct{})
	go func() {
		defer close(serverDone)
		c, err := listener.Accept()
		if err == nil {
			c.Close()
		}
	}()
	cfg := testConfig(t)
	cfg.Proxy = "socks5://" + listener.Addr().String()
	e := newEngine(cfg, true)
	if err = e.Start(); err != nil {
		t.Fatal(err)
	}
	in, peer := net.Pipe()
	defer peer.Close()
	entered, release := make(chan struct{}), make(chan struct{})
	e.handler.NewConnectionEx(context.Background(), in, M.ParseSocksaddr("198.18.0.2:1234"), M.ParseSocksaddr("198.19.0.1:443"), func(error) { close(entered); <-release })
	select {
	case <-entered:
	case <-time.After(time.Second):
		close(release)
		finishEngine(t, e)
		t.Fatal("callback did not run")
	}
	e.Stop()
	if e.Wait(20 * time.Millisecond) {
		t.Error("terminated before callback returned")
	}
	close(release)
	finishEngine(t, e)
	listener.Close()
	<-serverDone
}

func TestUpstreamOptionOverrides(t *testing.T) {
	c, err := parseConfig([]byte(`{"proxy":"socks5://127.0.0.1:1080","stack":"system","tun_options":{"Name":"native0","AutoRoute":true,"DNSMode":"native","EXP_ExternalConfiguration":false,"EXP_DisableDNSHijack":false,"Inet6Address":["fd00::1/64"],"StrictRoute":true},"stack_options":{"UDPTimeout":"250ms","ICMPTimeout":1000000000,"UDPMapping":2,"UDPFiltering":1,"UDPNATMax":7,"ForwarderBindInterface":true}}`))
	if err != nil {
		t.Fatal(err)
	}
	if c.TunOptions.Name != "native0" || !c.TunOptions.AutoRoute || c.TunOptions.EXP_ExternalConfiguration || c.TunOptions.EXP_DisableDNSHijack || c.TunOptions.DNSMode != "native" || !c.TunOptions.StrictRoute || len(c.TunOptions.Inet6Address) != 1 {
		t.Fatal("native device options changed")
	}
	if c.StackOptions.UDPTimeout != 250*time.Millisecond || c.StackOptions.ICMPTimeout != time.Second || c.StackOptions.UDPNATMax != 7 || c.StackOptions.UDPMapping != 2 || c.StackOptions.UDPFiltering != 1 || !c.StackOptions.ForwarderBindInterface {
		t.Fatal("native stack options changed")
	}
	for _, options := range []string{
		`"tun_options":{"mtu":1500}`, `"tun_options":{"MTU":true}`,
		`"tun_options":{"Logger":null}`, `"tun_options":{"InterfaceMonitor":{}}`,
		`"stack_options":{"TunOptions":{}}`, `"stack_options":{"Context":null}`,
		`"stack_options":{"UDPTimeout":"bad"}`, `"stack_options":{"UDPTimeout":0}`,
		`"stack_options":{"UDPMapping":3}`, `"tun_options":null`,
		`"interface_name":"compat0"`, `"tcpSendBufferSize":"1MB"`,
	} {
		if _, err := parseConfig([]byte(`{"proxy":"socks5://127.0.0.1:1080",` + options + `}`)); err == nil {
			t.Fatalf("invalid/legacy/runtime option accepted: %s", options)
		}
	}
}

func TestAutomaticNameSelection(t *testing.T) {
	for _, tc := range []struct {
		start int
		used  []string
		want  string
	}{
		{10, nil, "utun10"},
		{10, []string{"utun10", "utun11", "en0"}, "utun12"},
		{1024, []string{"utun1024", "utun10"}, "utun11"},
	} {
		var interfaces []net.Interface
		for _, name := range tc.used {
			interfaces = append(interfaces, net.Interface{Name: name})
		}
		got, err := availableTunName(interfaces, tc.start)
		if err != nil || got != tc.want {
			t.Fatalf("start %d: name %q, error %v; want %q", tc.start, got, err, tc.want)
		}
	}
	var interfaces []net.Interface
	for n := 10; n <= 1024; n++ {
		interfaces = append(interfaces, net.Interface{Name: "utun" + strconv.Itoa(n)})
	}
	if _, err := availableTunName(interfaces, 512); err == nil {
		t.Fatal("accepted exhausted name range")
	}
	c := testConfig(t)
	if c.TunOptions.Name != "" {
		t.Fatalf("default name %q; want automatic selection", c.TunOptions.Name)
	}
	c, err := parseConfig([]byte(`{"proxy":"socks5://127.0.0.1:1","tun_options":{"Name":"utun777"}}`))
	if err != nil || c.TunOptions.Name != "utun777" {
		t.Fatal("explicit name not preserved", err)
	}
}
