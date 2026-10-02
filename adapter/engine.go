// SPDX-License-Identifier: GPL-3.0-or-later
package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net"
	"os"
	"sync"
	"time"

	tun "github.com/sagernet/sing-tun"
	"github.com/sagernet/sing/common/control"
)

var processSlot struct {
	sync.Mutex
	active *Engine
}

type Engine struct {
	cfg                       Config
	ctx                       context.Context
	cancel                    context.CancelFunc
	mu                        sync.Mutex
	initMu                    sync.Mutex
	state                     string
	failure                   string
	done                      chan struct{}
	stopping                  bool
	device                    tun.Tun
	deviceName                string
	stack                     tun.Stack
	monitors                  []io.Closer
	deviceClosed, stackClosed bool
	handler                   *forwarder
	fake                      *memoryTun
	openDevice                func(Config) (tun.Tun, error)
	makeStack                 func(string, tun.StackOptions) (tun.Stack, error)
}

func newEngine(c Config, memory bool) *Engine {
	ctx, cancel := context.WithCancel(context.Background())
	e := &Engine{cfg: c, ctx: ctx, cancel: cancel, state: "created", done: make(chan struct{}), openDevice: openHostTun, makeStack: managedStack}
	if memory {
		e.fake = newMemoryTun()
		e.openDevice = func(Config) (tun.Tun, error) { return newPacketDevice(e, e.fake), nil }
	}
	return e
}

func safeCall(f func() error) (err error) {
	defer func() {
		if recover() != nil {
			err = errors.New("native operation panicked")
		}
	}()
	return f()
}

func (e *Engine) Start() error {
	e.initMu.Lock()
	defer e.initMu.Unlock()
	e.mu.Lock()
	if e.state != "created" {
		e.mu.Unlock()
		return errors.New("engine cannot be started again")
	}
	processSlot.Lock()
	if processSlot.active != nil {
		processSlot.Unlock()
		e.mu.Unlock()
		return errors.New("one active engine per process is allowed")
	}
	processSlot.active = e
	processSlot.Unlock()
	e.state = "starting"
	e.mu.Unlock()
	err := safeCall(func() error {
		if e.fake != nil && e.cfg.Stack != "gvisor" {
			return errors.New("memory TUN supports only gvisor")
		}
		cfg := e.cfg
		cfg.TunOptions.Logger = engineLogger{e}
		finder := control.NewDefaultInterfaceFinder()
		if err := finder.Update(); err != nil {
			return errors.New("interface discovery failed")
		}
		cfg.TunOptions.InterfaceFinder = finder
		if !cfg.TunOptions.EXP_ExternalConfiguration {
			monitor, err := tun.NewNetworkUpdateMonitor(engineLogger{e})
			if err != nil {
				return errors.New("network monitor construction failed")
			}
			e.monitors = append(e.monitors, monitor)
			if err = monitor.Start(); err != nil {
				return errors.New("network monitor start failed")
			}
			defaultMonitor, err := tun.NewDefaultInterfaceMonitor(monitor, engineLogger{e}, tun.DefaultInterfaceMonitorOptions{InterfaceFinder: finder})
			if err != nil {
				return errors.New("interface monitor construction failed")
			}
			e.monitors = append(e.monitors, defaultMonitor)
			if err = defaultMonitor.Start(); err != nil {
				return errors.New("interface monitor start failed")
			}
			cfg.TunOptions.InterfaceMonitor = defaultMonitor
		}
		if cfg.TunOptions.Name == "" && cfg.TunOptions.FileDescriptor == 0 {
			interfaces, err := net.Interfaces()
			if err != nil {
				return errors.New("TUN name interface discovery failed")
			}
			cfg.TunOptions.Name, err = availableTunName(interfaces, 10+rand.IntN(1015))
			if err != nil {
				return err
			}
		}
		raw, err := e.openDevice(cfg)
		e.device = raw
		if err != nil {
			return errors.New("TUN open failed; check device permissions and platform prerequisites")
		}
		name, err := raw.Name()
		if err != nil {
			return errors.New("TUN interface name lookup failed")
		}
		cfg.TunOptions.Name = name
		e.mu.Lock()
		e.deviceName = name
		e.handler = newForwarder(e)
		e.mu.Unlock()
		options := e.cfg.StackOptions
		options.Context, options.Tun, options.TunOptions = e.ctx, e.device, cfg.TunOptions
		options.Handler, options.Logger, options.InterfaceFinder = e.handler, engineLogger{e}, finder
		st, err := e.makeStack(e.cfg.Stack, options)
		if err != nil {
			return errors.New("stack construction failed")
		}
		e.stack = st
		if err = e.device.Start(); err != nil {
			return errors.New("TUN start failed")
		}
		if err = e.stack.Start(); err != nil {
			return errors.New("stack start failed")
		}
		return nil
	})
	if err != nil {
		e.fail(err.Error())
		e.Stop()
		return err
	}
	e.mu.Lock()
	if e.ctx.Err() == nil {
		e.state = "ready"
	}
	e.mu.Unlock()
	engineLogger{e}.Info()
	return nil
}

func (e *Engine) fail(message string) {
	e.mu.Lock()
	if e.failure == "" {
		e.failure = message
	}
	e.mu.Unlock()
}
func (e *Engine) Stop() {
	e.mu.Lock()
	select {
	case <-e.done:
		e.mu.Unlock()
		return
	default:
	}
	if e.stopping {
		e.mu.Unlock()
		return
	}
	e.stopping = true
	e.state = "stopping"
	e.cancel()
	e.mu.Unlock()
	go e.cleanup()
}

func (e *Engine) cleanup() {
	e.initMu.Lock()
	defer e.initMu.Unlock()
	// Close the device first to unblock readers. No lifecycle lock covers callbacks.
	var errs []error
	if e.handler != nil {
		e.handler.stop()
	}
	if e.device != nil && !e.deviceClosed {
		if err := safeCall(e.device.Close); err != nil {
			errs = append(errs, err)
		} else {
			e.deviceClosed = true
		}
	}
	if e.stack != nil && !e.stackClosed {
		if err := safeCall(e.stack.Close); err != nil {
			errs = append(errs, err)
		} else {
			e.stackClosed = true
		}
	}
	for i := len(e.monitors) - 1; i >= 0; i-- {
		if e.monitors[i] != nil {
			if err := safeCall(e.monitors[i].Close); err != nil {
				errs = append(errs, err)
			} else {
				e.monitors[i] = nil
			}
		}
	}
	if len(errs) > 0 {
		e.mu.Lock()
		e.failure = "native cleanup failed; resources retained, retry stop or terminate the exact child"
		e.state = "cleanup_failed"
		e.stopping = false
		e.mu.Unlock()
		return
	}
	if device, ok := e.device.(*packetDevice); ok {
		device.wait()
	}
	if e.handler != nil {
		e.handler.wait()
	}
	processSlot.Lock()
	if processSlot.active == e {
		processSlot.active = nil
	}
	processSlot.Unlock()
	e.mu.Lock()
	if e.failure != "" {
		e.state = "failed"
	} else {
		e.state = "stopped"
	}
	close(e.done)
	e.mu.Unlock()
}

func (e *Engine) Wait(timeout time.Duration) bool {
	select {
	case <-e.done:
		return true
	default:
	}
	if timeout < 0 {
		<-e.done
		return true
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	select {
	case <-e.done:
		return true
	case <-timer.C:
		return false
	}
}

type Snapshot struct {
	State      string `json:"state"`
	Error      string `json:"error"`
	Sessions   int    `json:"sessions"`
	DeviceName string `json:"device_name"`
}

func (e *Engine) snapshot() Snapshot {
	e.mu.Lock()
	defer e.mu.Unlock()
	n := 0
	if e.handler != nil {
		n = e.handler.count()
	}
	return Snapshot{e.state, e.failure, n, e.deviceName}
}

// Only redacted events enter stderr; upstream logger arguments can contain endpoints.
type engineLogger struct{ e *Engine }

func (l engineLogger) event(level string) {
	ranks := map[string]int{"trace": 0, "debug": 1, "info": 2, "warn": 3, "error": 4, "fatal": 5, "panic": 5, "silent": 6}
	if ranks[level] >= ranks[l.e.cfg.LogLevel] {
		fmt.Fprintln(os.Stderr, "sing_tun:", level, "native stack event (details redacted)")
	}
}
func (l engineLogger) Trace(...any) { l.event("trace") }
func (l engineLogger) Debug(...any) { l.event("debug") }
func (l engineLogger) Info(...any)  { l.event("info") }
func (l engineLogger) Warn(...any)  { l.event("warn") }
func (l engineLogger) Error(...any) {
	if l.e.ctx.Err() != nil {
		return
	}
	l.event("error")
	l.e.fail("packet stack reported an error")
	l.e.Stop()
}
func (l engineLogger) Fatal(...any) { l.Error() }
func (l engineLogger) Panic(...any) { l.Error() }
