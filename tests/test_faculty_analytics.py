import os
import tempfile
import importlib
import unittest
from datetime import date, timedelta


def setup_app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    upload_dir = tempfile.mkdtemp()
    os.environ["DATABASE_PATH"] = db_path
    os.environ["UPLOAD_FOLDER"] = upload_dir
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return module, module.app.test_client()


class FacultyAnalyticsTestCase(unittest.TestCase):
    def test_faculty_analytics_chart_and_dashboard(self):
        module, client = setup_app()
        conn = module.get_db()

        # Create faculty and student users
        faculty_email = "prof.watson@univ.edu"
        student_email = "ada.lovelace@univ.edu"

        client.post(
            "/signup",
            data={
                "name": "Prof Watson",
                "email": faculty_email,
                "password": "password123",
                "user_type": "faculty",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Ada Lovelace",
                "email": student_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )

        faculty_user = conn.execute("SELECT id FROM users WHERE email=?", (faculty_email,)).fetchone()
        student_user = conn.execute("SELECT id FROM users WHERE email=?", (student_email,)).fetchone()
        self.assertIsNotNone(faculty_user)
        self.assertIsNotNone(student_user)

        faculty_id = faculty_user["id"]
        student_id = student_user["id"]

        # Create classroom
        cur = conn.execute(
            "INSERT INTO classrooms (faculty_id, class_name, class_code) VALUES (?, ?, ?)",
            (faculty_id, "Data Science 101", "DS101"),
        )
        class_id = cur.lastrowid
        conn.execute(
            "INSERT INTO classroom_members (classroom_id, student_id) VALUES (?, ?)",
            (class_id, student_id),
        )

        # Insert classroom assignment and submission
        today_str = date.today().strftime("%Y-%m-%d")
        cur_assign = conn.execute(
            """INSERT INTO classroom_assignments (classroom_id, faculty_id, student_id, task_name, subject, deadline, status)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (class_id, faculty_id, student_id, "Assignment 1", "Data Science", today_str, "Completed"),
        )
        assign_id = cur_assign.lastrowid

        conn.execute(
            """INSERT INTO classroom_submissions (assignment_id, classroom_id, student_id, submission_text, status, submitted_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (assign_id, class_id, student_id, "My DS submission", "Submitted"),
        )

        # Insert quiz attempt by enrolled student
        conn.execute(
            """INSERT INTO quiz_attempts (user_id, source_name, total_questions, correct_answers, score, quiz_data, quiz_results)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (student_id, "DS Quiz", 5, 4, 80, "{}", "{}"),
        )
        conn.commit()

        # Test helper directly
        from utils.productivity_tracker import get_faculty_weekly_analytics

        analytics = get_faculty_weekly_analytics(faculty_id, conn)
        self.assertIsNotNone(analytics)
        self.assertEqual(len(analytics["dates"]), 7)
        self.assertEqual(len(analytics["labels"]), 7)
        self.assertEqual(len(analytics["assignments"]), 7)
        self.assertEqual(len(analytics["quizzes"]), 7)
        self.assertGreaterEqual(analytics["total_assignments"], 1)
        self.assertGreaterEqual(analytics["total_quizzes"], 1)

        # Login as faculty and fetch faculty dashboard
        client.post(
            "/login",
            data={"email": faculty_email, "password": "password123"},
            follow_redirects=True,
        )

        dash_resp = client.get("/faculty-dashboard")
        self.assertEqual(dash_resp.status_code, 200)
        html = dash_resp.data.decode("utf-8")

        self.assertIn("Class Analytics", html)
        self.assertIn("facultyAnalyticsChart", html)
        self.assertIn("facultyAnalyticsData", html)
        self.assertIn("Live Insights", html)
        self.assertIn("Weekly Submissions", html)
        self.assertIn("Quiz Attempts", html)
        # Now delete the classroom and verify the graph analytics reset to 0
        del_resp = client.post(
            f"/classrooms/{class_id}/delete",
            data={"confirm_name": "Data Science 101"},
            follow_redirects=True,
        )
        self.assertEqual(del_resp.status_code, 200)

        # Check helper returns 0s
        conn_after = module.get_db()
        analytics_after = get_faculty_weekly_analytics(faculty_id, conn_after)
        self.assertEqual(analytics_after["total_assignments"], 0)
        self.assertEqual(analytics_after["total_quizzes"], 0)
        self.assertEqual(analytics_after["assignments"], [0, 0, 0, 0, 0, 0, 0])
        self.assertEqual(analytics_after["quizzes"], [0, 0, 0, 0, 0, 0, 0])

        # Check faculty dashboard HTML renders clean 0s
        dash_after = client.get("/faculty-dashboard")
        self.assertEqual(dash_after.status_code, 200)
        html_after = dash_after.data.decode("utf-8")
        self.assertIn("Class Analytics", html_after)
        self.assertIn('"total_assignments": 0', html_after)
        self.assertIn('"total_quizzes": 0', html_after)
        conn_after.close()


if __name__ == "__main__":
    unittest.main()


