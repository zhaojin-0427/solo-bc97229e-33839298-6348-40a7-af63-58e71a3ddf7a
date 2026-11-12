"""发布校验单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "collector"))

from app.validation import validate_csv  # noqa: E402

GOOD = "﻿产线,日期,样品号,测量值\nA线,2026-11-10,A-001,12.34\nA线,2026-11-10,A-002,56.78\n"
BAD_LINE = "产线,日期,样品号,测量值\nA线,2026-11-10,A-001,12.34\nX线,2026-11-10,X-002,56.78\n"
BAD_DATE = "产线,日期,样品号,测量值\nA线,2026-11-10,A-001,12.34\nA线,2026-11-11,A-002,56.78\n"
HEADER_BAD = "line,date,sample,value\nA线,2026-11-10,A-001,12.34\n"
EMPTY_DATA = "产线,日期,样品号,测量值\n\n"
EMPTY_FILE = ""


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(__file__).parent / "_tmp_validation"
        self.tmp.mkdir(exist_ok=True)

    def tearDown(self):
        for f in self.tmp.glob("*"):
            f.unlink()
        self.tmp.rmdir()

    def _write(self, name, content):
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return p

    def test_good_file_passes(self):
        p = self._write("good.csv", GOOD)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertTrue(r.ok, r.message)
        self.assertEqual(r.row_count, 2)

    def test_bom_is_accepted(self):
        p = self._write("bom.csv", GOOD)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertTrue(r.ok)

    def test_wrong_line_rejected(self):
        p = self._write("badline.csv", BAD_LINE)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertFalse(r.ok)
        self.assertIn("产线", r.message)

    def test_wrong_date_rejected(self):
        p = self._write("baddate.csv", BAD_DATE)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertFalse(r.ok)
        self.assertIn("日期", r.message)

    def test_bad_header_rejected(self):
        p = self._write("header.csv", HEADER_BAD)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertFalse(r.ok)
        self.assertIn("表头", r.message)

    def test_empty_data_rejected(self):
        p = self._write("emptydata.csv", EMPTY_DATA)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertFalse(r.ok)
        self.assertIn("无数据", r.message)

    def test_empty_file_rejected(self):
        p = self._write("empty.csv", EMPTY_FILE)
        r = validate_csv(p, "A线", "2026-11-10")
        self.assertFalse(r.ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
