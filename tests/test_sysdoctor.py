"""Tests for sysdoctor: each check, orchestration, and CLI."""
from __future__ import annotations

import json
import shutil
import unittest
import urllib.request
from unittest import mock

import sysdoctor
from sysdoctor import (FAIL, PASS, WARN, Check, check_disk, check_dns,
                       check_http, check_load, check_tcp, run_checks,
                       worst_status)
from tests.support.fakes import capture_cli, closed_port, http_fake, tcp_listener


class DiskCheckTests(unittest.TestCase):
    def _fake_usage(self, used_pct):
        total = 100 * 1024 ** 3
        used = int(total * used_pct / 100)
        return shutil._ntuple_diskusage(total, used, total - used)

    def test_healthy_disk_passes(self):
        with mock.patch("shutil.disk_usage", return_value=self._fake_usage(50)):
            self.assertEqual(check_disk().status, PASS)

    def test_high_usage_warns(self):
        with mock.patch("shutil.disk_usage", return_value=self._fake_usage(90)):
            self.assertEqual(check_disk().status, WARN)

    def test_critical_usage_fails(self):
        with mock.patch("shutil.disk_usage", return_value=self._fake_usage(97)):
            self.assertEqual(check_disk().status, FAIL)

    def test_stat_error_fails(self):
        with mock.patch("shutil.disk_usage", side_effect=OSError("boom")):
            self.assertEqual(check_disk("/nope").status, FAIL)


class LoadCheckTests(unittest.TestCase):
    def test_low_load_passes(self):
        with mock.patch("os.cpu_count", return_value=4), \
                mock.patch("os.getloadavg", return_value=(1.0, 1.0, 1.0)):
            self.assertEqual(check_load().status, PASS)

    def test_equal_load_warns(self):
        with mock.patch("os.cpu_count", return_value=4), \
                mock.patch("os.getloadavg", return_value=(4.0, 3.0, 2.0)):
            self.assertEqual(check_load().status, WARN)

    def test_high_load_fails(self):
        with mock.patch("os.cpu_count", return_value=2), \
                mock.patch("os.getloadavg", return_value=(5.0, 4.0, 3.0)):
            self.assertEqual(check_load().status, FAIL)

    def test_no_getloadavg_passes(self):
        # Simulate a platform (e.g. Windows) where os.getloadavg is absent.
        with mock.patch("os.cpu_count", return_value=4), \
                mock.patch("sysdoctor.hasattr",
                           lambda obj, name: name != "getloadavg"):
            self.assertEqual(check_load().status, PASS)


class DnsCheckTests(unittest.TestCase):
    def test_resolution_passes(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            check = check_dns("example.com")
        self.assertEqual(check.status, PASS)
        self.assertIn("93.184.216.34", check.data["addresses"])

    def test_resolution_failure_fails(self):
        import socket
        with mock.patch("socket.getaddrinfo",
                        side_effect=socket.gaierror("no")):
            self.assertEqual(check_dns("nope.invalid").status, FAIL)


class TcpCheckTests(unittest.TestCase):
    def test_open_port_passes(self):
        with tcp_listener() as (host, port):
            self.assertEqual(check_tcp(host, port).status, PASS)

    def test_closed_port_fails(self):
        host, port = closed_port()
        self.assertEqual(check_tcp(host, port, timeout=1.0).status, FAIL)


class HttpCheckTests(unittest.TestCase):
    def _opener_for(self, base):
        def opener(request, timeout=10.0):
            # Rewrite only the path onto the local fake server's base URL.
            from urllib.parse import urlsplit
            path = urlsplit(request.full_url).path or "/"
            return urllib.request.urlopen(base + path, timeout=timeout)
        return opener

    def test_200_passes(self):
        with http_fake({"/health": (200, {}, "ok")}) as (base, _rec):
            check = check_http(base + "/health", opener=self._opener_for(base))
        self.assertEqual(check.status, PASS)

    def test_404_warns(self):
        with http_fake(default=(404, {}, "nope")) as (base, _rec):
            check = check_http(base + "/missing",
                               opener=self._opener_for(base))
        self.assertEqual(check.status, WARN)

    def test_500_fails(self):
        with http_fake({"/x": (500, {}, "boom")}) as (base, _rec):
            check = check_http(base + "/x", opener=self._opener_for(base))
        self.assertEqual(check.status, FAIL)

    def test_connection_error_fails(self):
        host, port = closed_port()
        check = check_http(f"http://{host}:{port}/", timeout=1.0)
        self.assertEqual(check.status, FAIL)


class OrchestrationTests(unittest.TestCase):
    def test_worst_status_picks_fail(self):
        checks = [Check("a", PASS, ""), Check("b", FAIL, ""),
                  Check("c", WARN, "")]
        self.assertEqual(worst_status(checks), FAIL)

    def test_worst_status_empty_is_pass(self):
        self.assertEqual(worst_status([]), PASS)

    def test_run_checks_includes_tcp_and_http(self):
        with tcp_listener() as (host, port):
            checks = run_checks(dns_host="example.com",
                                tcp_targets=[(host, port)])
        names = [c.name for c in checks]
        self.assertIn("disk", names)
        self.assertIn("load", names)
        self.assertTrue(any(n.startswith("tcp:") for n in names))


class ParseTargetTests(unittest.TestCase):
    def test_valid_target(self):
        self.assertEqual(sysdoctor._parse_tcp_target("db.host:5432"),
                         ("db.host", 5432))

    def test_missing_port_rejected(self):
        with self.assertRaises(ValueError):
            sysdoctor._parse_tcp_target("db.host")

    def test_non_numeric_port_rejected(self):
        with self.assertRaises(ValueError):
            sysdoctor._parse_tcp_target("db.host:abc")


class CLITests(unittest.TestCase):
    def test_report_runs(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            code, out = capture_cli(sysdoctor.main, ["--dns-host", "x.test"])
        self.assertIn("sysdoctor report", out)
        self.assertIn(code, (0, 1))

    def test_json_output(self):
        fake = [(2, 1, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=fake):
            code, out = capture_cli(sysdoctor.main,
                                    ["--dns-host", "x.test", "--json"])
        parsed = json.loads(out)
        self.assertIn("checks", parsed)
        self.assertIn("overall", parsed)

    def test_bad_tcp_target_exits_two(self):
        code, err = capture_cli(sysdoctor.main, ["--tcp", "nohost"])
        self.assertEqual(code, 2)
        self.assertIn("invalid tcp target", err)


if __name__ == "__main__":
    unittest.main()
