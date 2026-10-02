"""Control-pipe integration sketch. Caller owns host networking and escalation."""

from multiprocessing import get_context


def child(config_values, control):
    # Imports happen inside the child. Do not fork an initialized Go runtime.
    engine = None
    try:
        from sing_tun import Config, Engine

        engine = Engine(Config(**config_values))
        engine.start()
        if not engine.ready:
            raise RuntimeError("native startup canceled")
        control.send({"state": "ready", "device_name": engine.device_name})
        while not engine.wait(0.1):
            if control.poll():
                try:
                    command = control.recv()
                except EOFError:
                    command = "stop"
                if command == "stop":
                    engine.stop()
        engine.close()
        control.send({"state": "stopped"})
    except Exception:
        # No configuration or proxy URLs in diagnostics.
        try:
            control.send({"state": "failed", "error": "native TUN bridge failed"})
        except (EOFError, OSError):
            pass
        raise
    finally:
        try:
            if engine is not None:
                engine.close()
        finally:
            control.close()


def launch(config_values):
    ctx = get_context("spawn")
    parent, child_end = ctx.Pipe()
    process = ctx.Process(target=child, args=(config_values, child_end))
    try:
        process.start()
    except BaseException:
        parent.close()
        child_end.close()
        process.close()
        raise
    child_end.close()
    return process, parent


def shutdown(process, control):
    try:
        control.send("stop")
    except (EOFError, OSError):
        pass
    process.join(5)
    if process.is_alive():
        process.terminate()  # Exact owned child; Windows supported.
        process.join(3)
    if process.is_alive():
        process.kill()
        process.join(3)
    if process.is_alive():
        raise RuntimeError("owned child could not be reaped; retain ownership")
    control.close()
    process.close()
