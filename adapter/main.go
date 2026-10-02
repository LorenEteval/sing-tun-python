// SPDX-License-Identifier: GPL-3.0-or-later
package main

/*
#include <stdlib.h>
#include <stdint.h>
*/
import "C"
import (
	"encoding/json"
	"errors"
	"sync"
	"time"
	"unsafe"
)

func main() {}

//export st_validate
func st_validate(raw *C.char, size C.size_t) *C.char {
	if size > 65536 {
		return C.CString("configuration exceeds 64 KiB")
	}
	_, err := parseConfig(C.GoBytes(unsafe.Pointer(raw), C.int(size)))
	return cError(err)
}

//export st_abandon
func st_abandon(id C.uint64_t) {
	e, err := lookup(id)
	if err != nil {
		return
	}
	e.Stop()
	go func() { <-e.done; st_release(id) }()
}

var handles = struct {
	sync.Mutex
	next    uint64
	engines map[uint64]*Engine
}{engines: make(map[uint64]*Engine)}

func lookup(id C.uint64_t) (*Engine, error) {
	handles.Lock()
	defer handles.Unlock()
	e := handles.engines[uint64(id)]
	if e == nil {
		return nil, errors.New("engine handle is closed")
	}
	return e, nil
}
func cError(err error) *C.char {
	if err == nil {
		return nil
	}
	return C.CString(err.Error())
}

//export st_free
func st_free(p unsafe.Pointer) { C.free(p) }

//export st_create
func st_create(raw *C.char, size C.size_t, memory C.int, errOut **C.char) (id C.uint64_t) {
	defer func() {
		if recover() != nil {
			*errOut = C.CString("native configuration failed")
			id = 0
		}
	}()
	if size > 65536 {
		*errOut = C.CString("configuration exceeds 64 KiB")
		return 0
	}
	// GoBytes copies all C/C++ storage before the call returns.
	cfg, err := parseConfig(C.GoBytes(unsafe.Pointer(raw), C.int(size)))
	if err != nil {
		*errOut = cError(err)
		return 0
	}
	e := newEngine(cfg, memory != 0)
	handles.Lock()
	defer handles.Unlock()
	handles.next++
	handles.engines[handles.next] = e
	return C.uint64_t(handles.next)
}

//export st_start
func st_start(id C.uint64_t) *C.char {
	e, err := lookup(id)
	if err != nil {
		return cError(err)
	}
	err = e.Start()
	if err != nil && e.snapshot().Error != "" {
		e.Wait(5 * time.Second)
	}
	return cError(err)
}

//export st_stop
func st_stop(id C.uint64_t) *C.char {
	e, err := lookup(id)
	if err == nil {
		e.Stop()
	}
	return cError(err)
}

//export st_wait
func st_wait(id C.uint64_t, milliseconds C.int64_t) C.int {
	e, err := lookup(id)
	if err != nil {
		return -1
	}
	if e.Wait(time.Duration(milliseconds) * time.Millisecond) {
		return 1
	}
	return 0
}

//export st_snapshot
func st_snapshot(id C.uint64_t) *C.char {
	e, err := lookup(id)
	if err != nil {
		return C.CString(`{"state":"closed","error":"","sessions":0}`)
	}
	b, _ := json.Marshal(e.snapshot())
	return C.CString(string(b))
}

//export st_release
func st_release(id C.uint64_t) C.int {
	e, err := lookup(id)
	if err != nil {
		return 1
	}
	if !e.Wait(0) {
		return 0
	}
	handles.Lock()
	delete(handles.engines, uint64(id))
	handles.Unlock()
	return 1
}

//export st_inject
func st_inject(id C.uint64_t, p *C.char, size C.size_t) *C.char {
	e, err := lookup(id)
	if err != nil {
		return cError(err)
	}
	if e.fake == nil || size < 20 || size > C.size_t(e.cfg.TunOptions.MTU) {
		return C.CString("invalid diagnostic packet")
	}
	return cError(e.fake.inject(e.ctx, C.GoBytes(unsafe.Pointer(p), C.int(size))))
}

//export st_receive
func st_receive(id C.uint64_t, milliseconds C.int64_t, length *C.size_t) *C.char {
	e, err := lookup(id)
	if err != nil || e.fake == nil {
		return nil
	}
	timer := time.NewTimer(time.Duration(milliseconds) * time.Millisecond)
	defer timer.Stop()
	select {
	case b := <-e.fake.output:
		*length = C.size_t(len(b))
		return (*C.char)(C.CBytes(b))
	case <-timer.C:
		return nil
	case <-e.done:
		return nil
	}
}
