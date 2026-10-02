// SPDX-License-Identifier: GPL-3.0-or-later
#include <atomic>
#include <mutex>
#include <string>
#include <stdexcept>
#if defined(__MINGW32__) && defined(_M_ARM64)
#include <cstdint>
static inline std::uintptr_t getArm64ThreadPointer() {
    std::uintptr_t value;
    __asm__ __volatile__("mov %0, x18" : "=r"(value));
    return value;
}
#define __getReg(registerNumber) getArm64ThreadPointer()
#endif
#if defined(_WIN64)
#define _hypot hypot
#include <cmath>
#endif
#include <pybind11/pybind11.h>
#if defined(__MINGW32__) && defined(_M_ARM64)
#undef __getReg
#endif
#include "sing_tun.h"

namespace py = pybind11;
static void check(char *error) {
    if (error) {
        std::string text(error);
        st_free(error);
        throw std::runtime_error(text);
    }
}
static std::string take(char *value) {
    if (!value) return {};
    std::string result(value);
    st_free(value);
    return result;
}
class NativeEngine {
    std::atomic<uint64_t> handle{0};
    std::mutex close_mutex;
    std::string last = "{\"state\":\"closed\",\"error\":\"\",\"sessions\":0}";
public:
    NativeEngine(const std::string &config, bool memory) {
        char *error = nullptr;
        handle = st_create(const_cast<char *>(config.data()), config.size(), memory, &error);
        check(error);
    }
    ~NativeEngine() { auto id = handle.load(); if (id) st_abandon(id); }
    void start() { auto id=handle.load(); if (!id) throw std::runtime_error("engine is closed"); check(st_start(id)); }
    void stop() {
        auto id=handle.load(); if (!id) return;
        char *error=st_stop(id);
        if (error) {
            std::lock_guard<std::mutex> lock(close_mutex);
            if (!handle.load()) { st_free(error); return; }
        }
        check(error);
    }
    bool wait(int64_t ms) {
        auto id=handle.load(); if (!id) return true;
        int result=st_wait(id,ms);
        if (result >= 0) return result==1;
        // A concurrent close can remove the ID between load and native lookup.
        std::lock_guard<std::mutex> lock(close_mutex);
        return !handle.load();
    }
    std::string snapshot() {
        std::lock_guard<std::mutex> lock(close_mutex);
        auto id=handle.load(); return id ? take(st_snapshot(id)) : last;
    }
    bool close(int64_t ms) {
        // Wait outside close_mutex: snapshots and concurrent stop remain available.
        stop(); if (!wait(ms)) return false;
        std::lock_guard<std::mutex> lock(close_mutex);
        auto id=handle.load(); if (id) { last=take(st_snapshot(id)); if (!st_release(id)) return false; handle=0; }
        return true;
    }
    void inject(const std::string &packet) { check(st_inject(handle.load(),const_cast<char *>(packet.data()),packet.size())); }
    std::string receive(int64_t ms) { size_t n=0;char *p=st_receive(handle.load(),ms,&n);if (!p) return {};std::string b(p,n);st_free(p);return b; }
};
// Free-threaded wheels keep CPython's compatibility GIL. Native operations are
// thread-safe and release it; no claim of a fully GIL-free extension is made.
PYBIND11_MODULE(_native,m) {
    m.attr("__version__")=SING_TUN_VERSION;
    m.attr("__upstream_version__")=SING_TUN_UPSTREAM_VERSION;
    m.attr("__upstream_commit__")=SING_TUN_UPSTREAM_COMMIT;
    m.def("validate",[](const std::string &raw){check(st_validate(const_cast<char *>(raw.data()),raw.size()));});
    py::class_<NativeEngine>(m,"Engine")
        .def(py::init<const std::string &,bool>(),py::arg("config"),py::arg("memory")=false)
        .def("start",&NativeEngine::start,py::call_guard<py::gil_scoped_release>())
        .def("stop",&NativeEngine::stop,py::call_guard<py::gil_scoped_release>())
        .def("wait",&NativeEngine::wait,py::call_guard<py::gil_scoped_release>())
        .def("close",&NativeEngine::close,py::call_guard<py::gil_scoped_release>())
        .def("snapshot",&NativeEngine::snapshot,py::call_guard<py::gil_scoped_release>())
        .def("inject",[](NativeEngine &e,py::bytes data){std::string p=data;py::gil_scoped_release release;e.inject(p);})
        .def("receive",[](NativeEngine &e,int64_t ms){std::string p;{py::gil_scoped_release release;p=e.receive(ms);}return py::bytes(p);});
}
