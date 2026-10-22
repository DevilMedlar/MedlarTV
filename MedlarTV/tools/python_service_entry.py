"""Direct entry adapter for owned Python services; imports no Medlar packages.

The parent launcher owns the cleanup deadline. SIGBREAK interrupts Python on its
main thread once, allowing Waitress/asyncio to unwind without repeated requests
interrupting that cleanup. It cannot interrupt an arbitrary blocking C call.
"""
from pathlib import Path
import runpy
import signal
import sys


ROOT = Path(__file__).resolve().parents[2]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("libretranslate", "avatar", "avatar-console"):
        print("[SERVICE] Invalid service entry", flush=True)
        return 2
    service, options = args[0], args[1:]
    previous = None
    installed = False
    requested = False

    def request_stop(signum, frame):
        nonlocal requested
        if requested:
            return
        requested = True
        print(f"[SERVICE] Cooperative stop requested service={service}", flush=True)
        raise KeyboardInterrupt

    try:
        previous = signal.getsignal(signal.SIGBREAK)
        signal.signal(signal.SIGBREAK, request_stop)
        installed = True
    except (AttributeError, OSError, ValueError, RuntimeError) as exc:
        print(f"[SERVICE] Cooperative handler unavailable service={service} reason={type(exc).__name__}", flush=True)

    original_argv, original_path = sys.argv, sys.path[:]
    try:
        if service == "libretranslate":
            sys.argv = ["libretranslate.main", *options]
            sys.path[0:1] = [str(Path.cwd())]
            runpy.run_module("libretranslate.main", run_name="__main__", alter_sys=True)
        else:
            target = (ROOT / "MedlarTV" / "avatar" / "bridge" / "server.py" if service == "avatar"
                      else ROOT / "MedlarTV" / "avatar_client" / "console_client.py")
            sys.argv = [str(target), *options]
            sys.path[0:1] = [str(ROOT), str(target.parent)]
            runpy.run_path(str(target), run_name="__main__")
        print(f"[SERVICE] Entry returned service={service}", flush=True)
        return 0
    except KeyboardInterrupt:
        print(f"[SERVICE] Entry interrupted service={service}", flush=True)
        return 0
    finally:
        sys.argv = original_argv
        sys.path[:] = original_path
        if installed:
            try:
                signal.signal(signal.SIGBREAK, previous)
            except (AttributeError, OSError, ValueError, RuntimeError) as exc:
                print(f"[SERVICE] Handler restore unavailable service={service} reason={type(exc).__name__}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
