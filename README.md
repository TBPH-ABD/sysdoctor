# sysdoctor

A quick, read-only health check for a machine and its connectivity — the
diagnostics a support or help-desk engineer runs first when a user says "it's
slow" or "it can't connect". One command, a clear PASS / WARN / FAIL report, and
a JSON mode for a ticket or a dashboard.

## Why

When a problem comes in, you want the basics confirmed fast: is the disk full,
is the box overloaded, does DNS resolve, can it reach the service it needs?
`sysdoctor` runs those checks in one pass and tells you in plain language what is
healthy and what is not — no agent to install, no dependencies, nothing changed
on the machine.

## Checks

| Check | PASS / WARN / FAIL |
| --- | --- |
| **disk** | free space on a filesystem — WARN at 85% used, FAIL at 95% |
| **load** | 1-minute load average vs. CPU count — WARN at ratio ≥ 1.0, FAIL at ≥ 2.0 (POSIX) |
| **dns** | a hostname resolves — WARN if slower than 2 s, FAIL if it cannot resolve |
| **tcp** | a `host:port` accepts a connection — WARN if slow, FAIL if refused/timed out |
| **http** | a URL returns a good status — WARN on 4xx or slow, FAIL on 5xx or no response |

## Requirements

Python 3.10 or newer. No packages to install.

## Usage

```bash
# Basic local + DNS health
python3 sysdoctor.py

# Check a different filesystem and resolver target
python3 sysdoctor.py --disk-path /var --dns-host google.com

# Probe the services this box depends on
python3 sysdoctor.py --tcp db.internal:5432 --tcp cache.internal:6379

# Check an HTTP endpoint's health
python3 sysdoctor.py --http https://api.example.com/health

# JSON for a ticket or a monitoring job
python3 sysdoctor.py --http https://api.example.com/health --json
```

### Options

| Flag | Description | Default |
| --- | --- | --- |
| `--disk-path` | Filesystem to check for free space | `/` |
| `--dns-host` | Hostname to test resolution | `example.com` |
| `--tcp HOST:PORT` | TCP target to probe (repeatable) | — |
| `--http URL` | HTTP(S) URL to probe (repeatable) | — |
| `--json` | Emit the report as JSON | off |

## Example

```
$ python3 sysdoctor.py --tcp 1.1.1.1:53 --http https://example.com
sysdoctor report
  host     : web-01
  platform : Linux-6.1.0-amd64
  python   : 3.11.6  (4 CPU)

  [PASS] disk: 61.3% used, 42.0 GiB free on /
  [WARN] load: load 4.90 / 3.10 / 2.40 over 4 CPU(s) (1-min ratio 1.23)
  [PASS] dns: example.com resolved to 93.184.216.34 in 12 ms
  [PASS] tcp:1.1.1.1:53: connected to 1.1.1.1:53 in 8 ms
  [PASS] http:https://example.com: HTTP 200 in 240 ms

  overall: WARN  (4 pass, 1 warn, 0 fail)
```

Exits non-zero only when a check **FAILs**, so a cron or monitoring job can act
on a genuine outage while tolerating warnings.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Standard-library `unittest`. Network checks are exercised against local fake
servers and mocked resolvers, so the suite never depends on the internet.

## Scope

`sysdoctor` is read-only diagnostics. It makes outbound probes only to the DNS,
TCP, and HTTP targets you pass on the command line, and it never modifies the
system.

## License

MIT — see [LICENSE](LICENSE).
