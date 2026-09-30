#!/usr/bin/env python3
"""testbed command: break the testbed from a terminal (e.g. Coolify's terminal for the container).

  testbed                   interactive menu
  testbed status            health of this container (and the worker, from web)
  testbed ACTION            run an action here, e.g. `testbed unhealthy` or `testbed --unhealthy`
  testbed ACTION --worker   run it on the worker container (from web)
"""
import json
import sys
import urllib.error
import urllib.request

APP = "http://127.0.0.1:8080"
ACTIONS = [
    ("unhealthy", "Make unhealthy", "until the next restart"),
    ("unhealthy-persistent", "Make unhealthy, permanently", "survives restarts"),
    ("hang", "Hang the health check", "running, but the check times out"),
    ("crash", "Crash once", "exits, Docker restarts it"),
    ("crash-loop", "Crash loop", "crashes on the next 5 starts"),
    ("slow-start", "Slow start next time", "stays 'starting' for a while"),
    ("recover", "Recover", "clear every failure mode"),
]
NAMES = [a[0] for a in ACTIONS]


def call(method, path):
    req = urllib.request.Request(APP + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")
    except Exception:
        return None


def status_line():
    s = call("GET", "/status")
    if not s:
        return "no answer from the app (restarting?)"
    line = f"testbed {s['version']} ({s['role']}): {s['reason']}" + (" (health check hanging)" if s.get("hang") else "")
    if s["role"] == "web":
        w = s.get("worker")
        line += f" | worker: {w['reason'] if w else 'unreachable'}"
    return line


def act(name, worker=False):
    result = call("POST", f"/action/{name}" + ("?target=worker" if worker else ""))
    if result is None:
        return "Sent. The app is restarting." if name.startswith("crash") else "No answer from the app."
    return result.get("message", str(result))


def menu():
    worker = False
    while True:
        print()
        print(status_line())
        print(f"Target: {'worker container' if worker else 'this container'}")
        for i, (_, label, note) in enumerate(ACTIONS, 1):
            print(f"  {i}) {label:<30} {note}")
        print("  w) switch target    s) status    q) quit")
        try:
            choice = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("q", "quit", "exit"):
            return
        if choice == "w":
            worker = not worker
        elif choice.isdigit() and 1 <= int(choice) <= len(ACTIONS):
            print(act(ACTIONS[int(choice) - 1][0], worker))
        elif choice not in ("s", ""):
            print("Pick a number, w, s or q.")


def main(args):
    worker = "--worker" in args
    args = [a for a in args if a != "--worker"]
    if not args:
        return menu()
    name = args[0].lstrip("-")
    if name in ("h", "help"):
        print(__doc__.strip())
        print("\nActions: " + ", ".join(NAMES))
        return 0
    if name == "status":
        print(status_line())
        return 0
    if name not in NAMES:
        print(f"Unknown action '{args[0]}'. Actions: {', '.join(NAMES)}, status, help")
        return 2
    print(act(name, worker))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
