import csv
import json
from pathlib import Path
import tempfile
import unittest

from lms.passport import _academic_year, _grade_cells, _item_type, export_passport


class PassportExportTests(unittest.TestCase):
    def test_course_name_metadata(self) -> None:
        self.assertEqual(_academic_year("Экономика (2026/2027 модули: 1,2)"), "2026/2027")
        self.assertEqual(_academic_year("Курсовой проект 25-26 уч. г."), "2025/2026")

    def test_grade_rows_with_different_moodle_layouts(self) -> None:
        seven = [{"text": value} for value in ["Итоговая оценка за курс", "-", "6,00", "0–10", "60 %", "", "-"]]
        five = [{"text": value} for value in ["Тест", "- Анализ оценок", "0–10", "-", "0 %"]]
        four = [{"text": value} for value in ["Итого в категории", "6", "0–10", "-"]]
        self.assertEqual(_grade_cells(seven)["grade"], "6,00")
        self.assertEqual(_grade_cells(five)["grade"], "-")
        self.assertEqual(_grade_cells(four)["grade"], "6")
        self.assertEqual(_item_type("Вычисляемая оценка Итого в категории «Экзамен»"), "итог категории")

    def test_minimal_export(self) -> None:
        with tempfile.TemporaryDirectory() as source_dir, tempfile.TemporaryDirectory() as output_dir:
            source = Path(source_dir)
            output = Path(output_dir)
            (source / "courses" / "1").mkdir(parents=True)
            (source / "messages" / "conversations").mkdir(parents=True)
            (source / "snapshot.json").write_text(json.dumps({
                "finished_at": "2026-09-11T12:00:00+00:00", "errors": []
            }))
            (source / "courses" / "index.json").write_text(json.dumps([{
                "id": 1, "fullname": "Тест (2026/2027 модули: 1)",
                "startdate": 1788202800, "enddate": 1798747200, "visible": True,
                "viewurl": "https://example.test/course/1",
            }]))
            (source / "courses" / "1" / "page.json").write_text(json.dumps({"sections": []}))
            (source / "courses" / "1" / "events.json").write_text("[]")
            (source / "courses" / "1" / "grades.json").write_text(json.dumps({
                "available": True, "url": "https://example.test/grades/1", "tables": [{"rows": [[
                    {"text": "Итоговая оценка за курс", "links": []}, {"text": "-"},
                    {"text": "6,00"}, {"text": "0–10"}, {"text": "60 %"},
                    {"text": ""}, {"text": "-"},
                ]]}],
            }))
            (source / "notifications.json").write_text(json.dumps({"data": {"notifications": [], "unreadcount": 0}}))
            (source / "messages" / "conversations.json").write_text("[]")
            (source / "courses" / "1" / "state.json").write_text(json.dumps({
                "section": [{"id": "10", "number": 0, "title": "Main", "cmlist": ["99"]},
                            {"id": "11", "number": 1, "title": "Nested", "parentsectionid": "10", "cmlist": ["100", "101", "missing"]}],
                "cm": [{"id": "99", "sectionid": "10", "name": "Forum", "module": "forum", "url": "https://example.test/99"},
                       {"id": "100", "sectionid": "11", "name": "Homework", "module": "assign", "url": "https://example.test/100"},
                       {"id": "101", "sectionid": "11", "name": "Label", "module": "label"}]
            }))
            (source / "courses" / "1" / "page.json").write_text(json.dumps({"sections": [{
                "position": 0, "name": "Main", "activities": [
                    {"cmid": 99, "name": "Forum", "url": "https://example.test/99"},
                    {"cmid": 102, "name": "HTML only", "url": "https://example.test/102"}]}]}))
            result = export_passport(source, output, source / "workbook.json")
            with (output / "data" / "lms_activities.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual([r["cmid"] for r in rows], ["99", "100", "101", "102"])
            self.assertEqual(rows[1]["section_name"], "Nested")
            self.assertEqual(rows[2]["url"], "")
            (source / "courses" / "1" / "state.json").unlink()
            export_passport(source, output, source / "workbook.json")
            with (output / "data" / "lms_activities.csv").open(encoding="utf-8", newline="") as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 2)
            self.assertEqual(result["course_count"], 1)
            self.assertEqual(result["graded_item_count"], 1)
            self.assertTrue((output / "data" / "lms_courses.csv").exists())
            self.assertTrue((output / "LMS.md").exists())


if __name__ == "__main__":
    unittest.main()
