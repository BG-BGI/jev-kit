#!/usr/bin/env python3
"""The macOS scheduler: a systemd user unit, translated into a LaunchAgent.

The `.service` and `.timer` files in this repository stay the ONE description
of what runs and when. On macOS there is no systemd, so install/install.sh
hands the same files to this script, which reads the handful of keys the kit
actually uses and writes the equivalent launchd property list to
~/Library/LaunchAgents. Nothing here is a second copy to keep in sync.

    launchd.py render  UNIT.service [UNIT.timer] [--python PY]
    launchd.py install UNIT.service [UNIT.timer] [--python PY]
    launchd.py status  NAME
    launchd.py remove  NAME

What maps to what:

  ExecStart=            ProgramArguments (%h is $HOME). A bare python3 in the
                        unit -- `/usr/bin/python3` -- becomes --python: on
                        macOS /usr/bin/python3 is the Command Line Tools stub,
                        often older than the 3.10 the kit needs.
  WorkingDirectory=     WorkingDirectory
  Environment=          EnvironmentVariables
  Nice=                 Nice
  Restart=on-failure    KeepAlive {SuccessfulExit: false}, RunAtLoad
  RestartSec=           ThrottleInterval
  OnCalendar=hourly     StartCalendarInterval {Minute: 0}; launchd runs a
                        missed calendar slot on wake, which is what
                        Persistent=true asked for
  OnUnitActiveSec=      StartInterval
  OnBootSec=            RunAtLoad

Everything else (MemoryMax, CPUQuota, PrivateTmp, IOSchedulingClass,
NoNewPrivileges, RuntimeDirectory) has no LaunchAgent equivalent and is
dropped. The daemon creates its own 0700 socket directory, so losing
RuntimeDirectory= costs nothing.

Output goes to ~/Library/Logs/jev-kit/<name>.log. PATH is the launchd default
plus the per-user and Homebrew bin directories, because a LaunchAgent does not
inherit a login shell's PATH. Stdlib only.
"""
import argparse
import os
import plistlib
import re
import shlex
import subprocess
import sys

LABEL_PREFIX = "com.jev-kit."
DEFAULT_PATH_DIRS = ("~/.local/bin", "~/bin", "/opt/homebrew/bin",
                     "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")
_DURATION_RE = re.compile(r"^\s*(\d+)\s*(s|sec|min|m|h|hr|d)?\s*$")
_UNIT_SECONDS = {None: 1, "s": 1, "sec": 1, "m": 60, "min": 60,
                 "h": 3600, "hr": 3600, "d": 86400}
_CALENDARS = {
    "minutely": {"Second": 0},
    "hourly": {"Minute": 0},
    "daily": {"Hour": 0, "Minute": 0},
    "weekly": {"Weekday": 1, "Hour": 0, "Minute": 0},
}


def parse_unit(text):
    """{section: {key: [values]}} from a systemd unit file."""
    sections = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current.setdefault(key.strip(), []).append(value.strip())
    return sections


def seconds(value):
    """A systemd time span ("5min", "30min", "2min", "300") in seconds."""
    match = _DURATION_RE.match(value)
    if not match:
        raise ValueError("unsupported time span: %r" % value)
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2)]


def label_for(name):
    base = name.rsplit(".", 1)[0] if name.endswith((".service", ".timer")) else name
    return LABEL_PREFIX + base


def _expand(value, home):
    return value.replace("%h", home)


def _program_arguments(exec_start, home, python):
    argv = shlex.split(_expand(exec_start, home))
    if argv and python and os.path.basename(argv[0]).startswith("python3"):
        argv[0] = python
    return argv


def _default_path(home):
    return ":".join(d.replace("~", home, 1) for d in DEFAULT_PATH_DIRS)


def render(service_text, timer_text=None, name="job", home=None, python=None):
    """The LaunchAgent dict for one service (and its timer, if any)."""
    home = home or os.path.expanduser("~")
    service = parse_unit(service_text).get("Service", {})
    if "ExecStart" not in service:
        raise ValueError("%s has no ExecStart=" % name)
    base = label_for(name)[len(LABEL_PREFIX):]
    log = os.path.join(home, "Library", "Logs", "jev-kit", base + ".log")
    env = {"PATH": _default_path(home)}
    for assignment in service.get("Environment", []):
        for pair in shlex.split(_expand(assignment, home)):
            if "=" in pair:
                key, value = pair.split("=", 1)
                env[key] = value
    plist = {
        "Label": label_for(name),
        "ProgramArguments": _program_arguments(service["ExecStart"][-1], home, python),
        "EnvironmentVariables": env,
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "ProcessType": "Background",
    }
    if "WorkingDirectory" in service:
        plist["WorkingDirectory"] = _expand(service["WorkingDirectory"][-1], home)
    if "Nice" in service:
        plist["Nice"] = int(service["Nice"][-1])
    if service.get("Restart", [""])[-1] in ("on-failure", "always"):
        plist["RunAtLoad"] = True
        plist["KeepAlive"] = ({"SuccessfulExit": False}
                              if service["Restart"][-1] == "on-failure" else True)
        if "RestartSec" in service:
            plist["ThrottleInterval"] = seconds(service["RestartSec"][-1])
    if timer_text is not None:
        _apply_timer(plist, parse_unit(timer_text).get("Timer", {}))
    elif "KeepAlive" not in plist:
        plist["RunAtLoad"] = True
    return plist


def _apply_timer(plist, timer):
    if "OnCalendar" in timer:
        spec = timer["OnCalendar"][-1].lower()
        if spec not in _CALENDARS:
            raise ValueError("unsupported OnCalendar=%s" % spec)
        plist["StartCalendarInterval"] = dict(_CALENDARS[spec])
    if "OnUnitActiveSec" in timer:
        plist["StartInterval"] = seconds(timer["OnUnitActiveSec"][-1])
    if "OnBootSec" in timer:
        plist["RunAtLoad"] = True


def agents_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), "Library", "LaunchAgents")


def plist_path(name, home=None):
    return os.path.join(agents_dir(home), label_for(name) + ".plist")


def _domain():
    return "gui/%d" % os.getuid()


def _launchctl(*args):
    return subprocess.run(("launchctl",) + args, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


def install(service, timer=None, python=None):
    name = os.path.basename(service)
    with open(service) as f:
        service_text = f.read()
    timer_text = None
    if timer:
        with open(timer) as f:
            timer_text = f.read()
    plist = render(service_text, timer_text, name=name, python=python)
    os.makedirs(os.path.dirname(plist["StandardOutPath"]), exist_ok=True)
    os.makedirs(agents_dir(), exist_ok=True)
    path = plist_path(name)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    os.chmod(path, 0o644)
    _launchctl("bootout", "%s/%s" % (_domain(), plist["Label"]))
    result = _launchctl("bootstrap", _domain(), path)
    if result.returncode != 0:
        print("launchctl bootstrap %s failed: %s" % (path, result.stdout.strip()),
              file=sys.stderr)
        return 1
    print("%s -> %s" % (plist["Label"], path))
    return 0


def status(name):
    """0 and one line when the agent is loaded, 1 when it is not."""
    label = label_for(name)
    result = _launchctl("print", "%s/%s" % (_domain(), label))
    if result.returncode != 0:
        print("%s: not loaded" % label)
        return 1
    fields = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key in ("state", "last exit code", "pid"):
            fields.setdefault(key, value)
    print("%s: %s" % (label, ", ".join("%s=%s" % kv for kv in sorted(fields.items()))
                      or "loaded"))
    return 0


def remove(name):
    label = label_for(name)
    _launchctl("bootout", "%s/%s" % (_domain(), label))
    path = plist_path(name)
    if os.path.exists(path):
        os.remove(path)
    print("%s removed" % label)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for cmd in ("render", "install"):
        p = sub.add_parser(cmd)
        p.add_argument("service")
        p.add_argument("timer", nargs="?")
        p.add_argument("--python", default=sys.executable)
    for cmd in ("status", "remove"):
        sub.add_parser(cmd).add_argument("name")
    args = parser.parse_args(argv)
    if args.cmd == "render":
        with open(args.service) as f:
            service_text = f.read()
        timer_text = open(args.timer).read() if args.timer else None
        plist = render(service_text, timer_text, name=os.path.basename(args.service),
                       python=args.python)
        sys.stdout.write(plistlib.dumps(plist).decode())
        return 0
    if args.cmd == "install":
        return install(args.service, args.timer, python=args.python)
    if args.cmd == "status":
        return status(args.name)
    return remove(args.name)


if __name__ == "__main__":
    sys.exit(main())
