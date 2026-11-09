"""Offline checks for the task form, source restrictions and preflight."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from download_imagery import main, make_plan, parse_args
from imagery_result import download_and_verify
from imagery_dates import candidate_days, capture_day
from imagery_sources import SourceRedirect, check_url


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.form = self.root / "request.json"
        self.args = ["--request", str(self.form)]
        self.task = {"来源": "esri", "成像日期": "2026-10-08", "范围": [120, 30, 120.05, 30.03],
                     "分辨率": 10, "输出目录": str(self.root / "out")}

    def write(self, **updates):
        self.form.write_text(json.dumps(dict(self.task, **updates)), encoding="utf8")

    def legacy_args(self, *options):
        return ["--bbox", "120", "30", "120.05", "30.03", "--resolution", "10",
                "--output-dir", str(self.root / "out"), *options]

    def test_form_requires_a_full_calendar_date(self):
        for value in ("不限", "2025", "2026-02-29", "2026-1-2", 20261008):
            self.write(成像日期=value)
            with self.subTest(value=value), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                parse_args(self.args)

    def test_legacy_annual_layer_and_offline_plan(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(self.legacy_args("--source", "eox", "--date", "2025", "--dry-run")), 0)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["layer"], "s2cloudless-2025_3857")
        self.assertEqual(plan["grid"]["size"], [557, 386])
        self.assertFalse(Path(plan["work_dir"]).exists())

    def test_preview_resolves_a_sixteen_day_window_offline(self):
        self.task.pop("来源")
        self.write()
        with patch("download_imagery.coverage_date") as fetch, contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(self.args + ["--dry-run"]), 0)
            fetch.assert_not_called()
        days = json.loads(out.getvalue())["candidate_dates"]
        self.assertEqual((len(days), days[0], days[-1]), (16, "2026-10-08", "2026-09-23"))
        self.assertEqual(candidate_days("2024-03-01")[1], "2024-02-29")
        self.assertEqual(candidate_days("2026-01-01")[1], "2025-12-31")

    def test_stop_on_first_verified_day_or_exhaust_window(self):
        self.task.pop("来源")
        self.write()
        for actual, offset in [("2026-10-08", 0), ("2026-10-06", 2), ("2026-09-23", 15),
                               ("2026-09-22", None), ("2026-10-09", None), (None, None)]:
            evidence = {"acquisition_date": actual, "footprint_sha256": "test"}
            with self.subTest(actual=actual), patch("download_imagery.coverage_date", return_value=evidence), \
                    patch("download_imagery.gdal_modules"), contextlib.redirect_stdout(io.StringIO()) as out, \
                    patch("download_imagery.download_and_verify", return_value=0) as fetch:
                code = main(self.args)
                if offset is None:
                    self.assertNotEqual(code, 0)
                    fetch.assert_not_called()
                    self.assertEqual(len(json.loads(out.getvalue())["checked_dates"]), 16)
                else:
                    self.assertEqual(code, 0)
                    fetch.assert_called_once()
                    plan = fetch.call_args.args[0]
                    self.assertEqual((plan["lookback_days"], plan["acquisition_date"]), (offset, actual))
                    self.assertEqual(plan["checked_dates"], candidate_days("2026-10-08")[:offset+1])

    def test_failed_day_is_not_reported_as_success_or_absence(self):
        self.write()
        evidence = {"acquisition_date": "2026-10-08", "footprint_sha256": "test"}
        with patch("download_imagery.coverage_date", return_value=evidence), patch("download_imagery.gdal_modules"), \
                patch("download_imagery.download_and_verify", return_value=1), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(main(self.args), 0)
        result = json.loads(out.getvalue())
        self.assertEqual((result["status"], len(result["checked_dates"])), ("DOWNLOAD_FAILED", 16))

    def test_date_evidence_disagrees_or_is_missing(self):
        self.assertIsNone(capture_day({"SRC_DATE": 20250105, "SRC_DATE2": 1736121600000}))
        self.assertIsNone(capture_day({"SRC_DATE": None, "SRC_DATE2": None}))
        self.assertEqual(capture_day({"SRC_DATE": 20250106, "SRC_DATE2": 1736121600000}), "2025-01-06")

    def test_multi_source_continues_after_existing_and_failed_products(self):
        self.write()
        existing = Path(make_plan(parse_args(self.legacy_args("--source","esri")))["output"])
        existing.parent.mkdir()
        existing.write_bytes(b"existing")
        self.task.pop("来源")
        self.write()
        with patch("download_imagery.gdal_modules"), \
                patch("download_imagery.download_and_verify", side_effect=[1, 0]) as fetch, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertNotEqual(main(self.legacy_args()), 0)
        summary = json.loads(out.getvalue())
        self.assertEqual(summary["status"], "PARTIAL")
        self.assertEqual([r["status"] for r in summary["results"]], ["OUTPUT_EXISTS", "DOWNLOAD_FAILED", "SUCCESS"])
        self.assertEqual([c.args[0]["source"] for c in fetch.call_args_list], ["eox", "bing"])
        self.assertEqual(existing.read_bytes(), b"existing")

    def test_invalid_form_combinations(self):
        for updates in ({"来源": "other"}, {"来源": "bing"}, {"成像日期": "2025"},
                        {"成像日期": "2026-02-30"}, {"分辨率": True}, {"自定义地址": "x"},
                        {"参考栅格": str(self.root / "ref.tif")}, {"输出目录": "relative"}):
            with self.subTest(updates=updates):
                self.write(**updates)
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    parse_args(self.args)

    def test_limits_and_output_conflicts(self):
        for options, code in ((["--source", "esri", "--max-pixels", "1"], "PIXEL_LIMIT"),
                              (["--source", "bing", "--zoom", "20", "--max-blocks", "1"], "BLOCK_LIMIT")):
            with self.assertRaisesRegex(ValueError, code):
                make_plan(parse_args(self.legacy_args(*options)))
        self.write()
        plan = make_plan(parse_args(self.args))
        target = Path(plan["receipt"])
        target.parent.mkdir()
        target.write_bytes(b"existing")
        with self.assertRaisesRegex(FileExistsError, "OUTPUT_EXISTS"):
            make_plan(parse_args(self.args))
        self.assertEqual(target.read_bytes(), b"existing")

    def test_source_urls_and_redirects(self):
        source = "http://ecn.t3.tiles.virtualearth.net/tiles/a21.jpeg?g=1"
        req = Request(source)
        redirect = SourceRedirect("bing")
        self.assertIsNotNone(redirect.redirect_request(req, None, 301, "", {}, source.replace("http:", "https:")))
        for replacement in (source.replace("ecn.t3.", "other."), source.replace("a21.", "a22.")):
            with self.assertRaises(ValueError):
                redirect.redirect_request(req, None, 302, "", {}, replacement)
        for provider in ("esri", "eox", "bing"):
            with self.assertRaises(ValueError):
                check_url(provider, "https://example.invalid/tile.jpg")

    def test_failed_download_cannot_publish_outputs(self):
        args = self.legacy_args("--source", "eox", "--date", "2025")
        plan = make_plan(parse_args(args))
        class Failure:
            returncode = 1
        def fail(command, **kwargs):
            for flag in ("--out", "--raw-vrt"):
                Path(command[command.index(flag) + 1]).write_bytes(b"incomplete")
            return Failure()
        with patch("imagery_result.subprocess.run", side_effect=fail), contextlib.redirect_stdout(io.StringIO()):
            self.assertNotEqual(download_and_verify(plan, None), 0)
        self.assertFalse(Path(plan["raw_vrt"]).exists())
        self.assertEqual(list(Path(plan["output"]).parent.iterdir()), [])
        self.assertEqual(make_plan(parse_args(args))["output"], plan["output"])


if __name__ == "__main__":
    unittest.main()
