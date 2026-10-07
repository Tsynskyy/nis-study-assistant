import unittest

from lms.parsers import parse_course_links, parse_course_page, parse_notification_page, parse_tables


class ParserTests(unittest.TestCase):
    def test_parse_course_links_deduplicates(self) -> None:
        html = """
        <a href="/course/view.php?id=17">Course A</a>
        <a href="https://edu.hse.ru/course/view.php?id=17">Course A again</a>
        <a href="/course/view.php?id=42">Course B</a>
        """
        self.assertEqual(
            parse_course_links(html, "https://edu.hse.ru"),
            [
                {"id": 17, "fullname": "Course A", "viewurl": "https://edu.hse.ru/course/view.php?id=17"},
                {"id": 42, "fullname": "Course B", "viewurl": "https://edu.hse.ru/course/view.php?id=42"},
            ],
        )

    def test_parse_course_page_activities(self) -> None:
        html = """
        <h1>Test course</h1>
        <li class="section"><h3 class="sectionname">Week 1</h3>
          <a href="/mod/assign/view.php?id=99">Homework</a>
        </li>
        """
        value = parse_course_page(html, "https://edu.hse.ru/course/view.php?id=3")
        self.assertEqual(value["title"], "Test course")
        self.assertEqual(value["sections"][0]["activities"][0]["module"], "assign")
        self.assertEqual(value["sections"][0]["activities"][0]["cmid"], 99)


    def test_parse_tables_preserves_cells_and_links(self) -> None:
        html = '<table><caption>Grades</caption><tr><th>Item</th><td><a href="/x">8</a></td></tr></table>'
        tables = parse_tables(html, "https://edu.hse.ru/report")
        self.assertEqual(tables[0]["caption"], "Grades")
        self.assertEqual(tables[0]["rows"][0][1]["text"], "8")
        self.assertEqual(tables[0]["rows"][0][1]["links"][0]["url"], "https://edu.hse.ru/x")


    def test_parse_notification_page(self) -> None:
        html = """
        <title>Notifications</title>
        <div class="notification"><a href="/message/1">New grade</a><time>Today</time></div>
        """
        value = parse_notification_page(html, "https://edu.hse.ru/message/output/popup/notifications.php")
        self.assertEqual(value["items"][0]["text"], "New grade Today")
        self.assertEqual(value["items"][0]["url"], "https://edu.hse.ru/message/1")


if __name__ == "__main__":
    unittest.main()
