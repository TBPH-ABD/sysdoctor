#!/usr/bin/env python3
"""sysdoctor - a quick health check for a machine and its connectivity.

Runs a battery of read-only diagnostics a help-desk or support engineer reaches
for first: disk headroom, load relative to CPU count, DNS resolution, outbound
TCP reachability, and HTTP endpoint health. Each check returns PASS, WARN, or
FAIL with a plain-language reason, and the whole run can be printed as a report
or emitted as JSON for a ticket or a dashboard.

Everything here is standard library and read-only. sysdoctor inspects the
system and makes outbound network probes to the targets you give it; it changes
nothing and installs nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_RANK = {FAIL: 0, WARN: 1, PASS: 2}


class Check:
    """The outcome of a single diagnostic."""

    __slots__ = ("name", "status", "detail", "data")

    def __init__(self, name, status, detail, data=None):
        self.name = name
        self.status = status
        self.detail = detail
        self.data = data or {}

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status,
                "detail": self.detail, "data": self.data}


# ----------------------------------------------------------------------------
# Local checks
# ----------------------------------------------------------------------------

def check_disk(path: str = "/", warn_pct: float = 85.0,
               fail_pct: float = 95.0) -> Check:
    """Check disk usage of the filesystem holding ``path``."""
    try:
        usage = shutil.disk_usage(path)
    except OSError as exc:
        return Check("disk", FAIL, f"cannot stat {path}: {exc}")
    used_pct = usage.used / usage.total * 100 if usage.total else 0.0
    free_gb = usage.free / 1024 ** 3
    detail = (f"{used_pct:.1f}% used, {free_gb:.1f} GiB free on {path}")
    data = {"used_pct": round(used_pct, 1), "free_gb": round(free_gb, 2),
            "path": path}
    if used_pct >= fail_pct:
        return Check("disk", FAIL, detail, data)
    if used_pct >= warn_pct:
        return Check("disk", WARN, detail, data)
    return Check("disk", PASS, detail, data)


def check_load() -> Check:
    """Check 1-minute load average against the CPU count (POSIX only)."""
    cpus = os.cpu_count() or 1
    if not hasattr(os, "getloadavg"):
        return Check("load", PASS,
                     f"load average not available on this platform; "
                     f"{cpus} CPU(s)", {"cpus": cpus})
    try:
        one, five, fifteen = os.getloadavg()
    except OSError as exc:
        return Check("load", WARN, f"cannot read load average: {exc}")
    ratio = one / cpus
    detail = (f"load {one:.2f} / {five:.2f} / {fifteen:.2f} "
              f"over {cpus} CPU(s) (1-min ratio {ratio:.2f})")
    data = {"load1": round(one, 2), "cpus": cpus, "ratio": round(ratio, 2)}
    if ratio >= 2.0:
        return Check("load", FAIL, detail, data)
    if ratio >= 1.0:
        return Check("load", WARN, detail, data)
    return Check("load", PASS, detail, data)


def check_dns(hostname: str = "example.com") -> Check:
    """Check that a hostname resolves, and time how long it takes."""
    start = time.monotonic()
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        return Check("dns", FAIL, f"cannot resolve {hostname}: {exc}",
                     {"hostname": hostname})
    elapsed_ms = (time.monotonic() - start) * 1000
    addresses = sorted({info[4][0] for info in infos})
    detail = (f"{hostname} resolved to {', '.join(addresses[:3])} "
              f"in {elapsed_ms:.0f} ms")
    data = {"hostname": hostname, "addresses": addresses,
            "ms": round(elapsed_ms, 1)}
    if elapsed_ms >= 2000:
        return Check("dns", WARN, detail, data)
    return Check("dns", PASS, detail, data)


def check_tcp(host: str, port: int, timeout: float = 5.0) -> Check:
    """Check that a TCP connection to host:port can be established."""
    name = f"tcp:{host}:{port}"
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed_ms = (time.monotonic() - start) * 1000
    except (socket.timeout, TimeoutError):
        return Check(name, FAIL, f"timed out after {timeout:.0f}s",
                     {"host": host, "port": port})
    except OSError as exc:
        return Check(name, FAIL, f"connection refused or unreachable: {exc}",
                     {"host": host, "port": port})
    detail = f"connected to {host}:{port} in {elapsed_ms:.0f} ms"
    data = {"host": host, "port": port, "ms": round(elapsed_ms, 1)}
    if elapsed_ms >= 1000:
        return Check(name, WARN, detail, data)
    return Check(name, PASS, detail, data)


def check_http(url: str, timeout: float = 10.0,
               opener=None) -> Check:
    """Check that an HTTP(S) URL responds with a non-error status."""
    opener = opener or urllib.request.urlopen
    name = f"http:{url}"
    request = urllib.request.Request(url, method="GET",
                                     headers={"User-Agent": "sysdoctor"})
    start = time.monotonic()
    try:
        with opener(request, timeout=timeout) as response:
            status = response.status
            elapsed_ms = (time.monotonic() - start) * 1000
    except urllib.error.HTTPError as exc:
        exc.close()  # HTTPError holds an open buffer; close it.
        elapsed_ms = (time.monotonic() - start) * 1000
        data = {"url": url, "status": exc.code, "ms": round(elapsed_ms, 1)}
        status_class = FAIL if exc.code >= 500 else WARN
        return Check(name, status_class,
                     f"HTTP {exc.code} in {elapsed_ms:.0f} ms", data)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        return Check(name, FAIL, f"request failed: {exc}", {"url": url})
    detail = f"HTTP {status} in {elapsed_ms:.0f} ms"
    data = {"url": url, "status": status, "ms": round(elapsed_ms, 1)}
    if status >= 400:
        return Check(name, WARN, detail, data)
    if elapsed_ms >= 3000:
        return Check(name, WARN, detail, data)
    return Check(name, PASS, detail, data)


def system_info() -> dict:
    """Collect non-sensitive system identity for the report header."""
    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpus": os.cpu_count() or 1,
    }


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def run_checks(disk_path: str = "/", dns_host: str = "example.com",
               tcp_targets=None, http_targets=None) -> list[Check]:
    """Run the full battery and return the checks in run order."""
    checks = [check_disk(disk_path), check_load(), check_dns(dns_host)]
    for host, port in (tcp_targets or []):
        checks.append(check_tcp(host, port))
    for url in (http_targets or []):
        checks.append(check_http(url))
    return checks


def worst_status(checks: list[Check]) -> str:
    """Return the worst status across all checks (FAIL < WARN < PASS)."""
    if not checks:
        return PASS
    return min((c.status for c in checks), key=lambda s: _RANK.get(s, 9))


def _parse_tcp_target(spec: str) -> tuple[str, int]:
    """Parse a 'host:port' string, raising ValueError on bad input."""
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise ValueError(f"invalid tcp target {spec!r}; expected host:port")
    return host, int(port)


def _print_report(info: dict, checks: list[Check]) -> None:
    print("sysdoctor report")
    print(f"  host     : {info['hostname']}")
    print(f"  platform : {info['platform']}")
    print(f"  python   : {info['python']}  ({info['cpus']} CPU)")
    print()
    for check in checks:
        print(f"  [{check.status}] {check.name}: {check.detail}")
    print()
    summary = worst_status(checks)
    counts = {s: sum(1 for c in checks if c.status == s)
              for s in (PASS, WARN, FAIL)}
    print(f"  overall: {summary}  "
          f"({counts[PASS]} pass, {counts[WARN]} warn, {counts[FAIL]} fail)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sysdoctor",
        description="Quick read-only health check of a machine and its "
                    "connectivity.")
    parser.add_argument("--disk-path", default="/",
                        help="filesystem to check for free space (default: /)")
    parser.add_argument("--dns-host", default="example.com",
                        help="hostname to test DNS resolution (default: "
                             "example.com)")
    parser.add_argument("--tcp", action="append", default=[], metavar="HOST:PORT",
                        help="TCP target to probe; repeatable")
    parser.add_argument("--http", action="append", default=[], metavar="URL",
                        help="HTTP(S) URL to probe; repeatable")
    parser.add_argument("--json", action="store_true",
                        help="emit the report as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        tcp_targets = [_parse_tcp_target(spec) for spec in args.tcp]
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    info = system_info()
    checks = run_checks(disk_path=args.disk_path, dns_host=args.dns_host,
                        tcp_targets=tcp_targets, http_targets=args.http)

    if args.json:
        print(json.dumps({"system": info,
                          "overall": worst_status(checks),
                          "checks": [c.as_dict() for c in checks]}, indent=2))
    else:
        _print_report(info, checks)

    # Non-zero exit if anything FAILed, so a monitoring job can act on it.
    return 1 if worst_status(checks) == FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
