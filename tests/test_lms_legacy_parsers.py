import unittest

from lms.legacy_parsers import (
    parse_form_fields,
    parse_gradebook,
    parse_legacy_courses,
    parse_leaf_tables,
)


class LegacyParserTests(unittest.TestCase):
    def test_leaf_tables_skip_layout_ancestors(self) -> None:
        html = """
        <table id="layout"><tr><td><table id="data"><tr><th>Name</th></tr><tr><td>A</td></tr></table></td></tr></table>
        """
        tables = parse_leaf_tables(html, "https://lms.hse.ru/student.php")
        self.assertEqual(len(tables), 1)
        self.assertEqual(tables[0]["id"], "data")
        self.assertEqual(tables[0]["rows"][1][0]["text"], "A")

    def test_password_field_is_never_exported(self) -> None:
        html = """
        <form><label for="name">Name</label><input id="name" name="name" value="Roman">
        <input name="password" type="password" value="secret"></form>
        """
        fields = parse_form_fields(html)
        self.assertEqual(fields, [{"name": "name", "label": "Name", "value": "Roman", "display": "Roman"}])

    def test_gradebook_counts_real_rows_only(self) -> None:
        html = """
        <div id="gradebookBlock"><div id="gradebookSumHours">Credits: 3</div><table>
        <thead><tr><th>Course</th><th>Grade</th></tr></thead>
        <tbody><tr><td>Math</td><td>8</td></tr><tr><td colspan="2" class="sortedTableFooter">&nbsp;</td></tr></tbody>
        </table></div>
        """
        value = parse_gradebook(html, "https://lms.hse.ru/student.php")
        self.assertEqual(value["row_count"], 1)
        self.assertEqual(value["summary"], "Credits: 3")

    def test_course_links(self) -> None:
        html = '<a href="/student.php?lessons_ID=28355">Course</a>'
        self.assertEqual(
            parse_legacy_courses(html, "https://lms.hse.ru/student.php"),
            [{"id": 28355, "name": "Course", "url": "https://lms.hse.ru/student.php?lessons_ID=28355"}],
        )


if __name__ == "__main__":
    unittest.main()
