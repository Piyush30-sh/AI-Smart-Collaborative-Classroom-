import os
import tempfile
import importlib
import unittest


class TestStudentsRoster(unittest.TestCase):
    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        self.upload_dir = tempfile.mkdtemp()
        os.environ["DATABASE_PATH"] = self.db_path
        os.environ["UPLOAD_FOLDER"] = self.upload_dir

        self.module = importlib.import_module("app")
        self.module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
        self.module.DATABASE = self.db_path
        self.module.UPLOAD_FOLDER = self.upload_dir
        self.module.init_db()
        self.client = self.module.app.test_client()

        self.faculty_email = "prof.gauss@university.edu"
        self.student1_email = "ada.lovelace@university.edu"
        self.student2_email = "grace.hopper@university.edu"

        # Register Faculty & 2 Students
        self.client.post(
            "/signup",
            data={
                "name": "Prof Gauss",
                "email": self.faculty_email,
                "password": "password123",
                "user_type": "faculty",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/signup",
            data={
                "name": "Ada Lovelace",
                "email": self.student1_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )
        self.client.post(
            "/signup",
            data={
                "name": "Grace Hopper",
                "email": self.student2_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )

        # Faculty creates 2 Classrooms: Math 101 & Physics 101
        self.client.post(
            "/login",
            data={"email": self.faculty_email, "password": "password123", "user_type": "faculty"},
            follow_redirects=True,
        )
        self.client.post("/classrooms", data={"class_name": "Math 101"}, follow_redirects=True)
        self.client.post("/classrooms", data={"class_name": "Physics 101"}, follow_redirects=True)

        conn = self.module.get_db()
        math_class = conn.execute("SELECT * FROM classrooms WHERE class_name='Math 101'").fetchone()
        physics_class = conn.execute("SELECT * FROM classrooms WHERE class_name='Physics 101'").fetchone()
        self.math_code = math_class["class_code"]
        self.physics_code = physics_class["class_code"]
        conn.close()
        self.client.get("/logout")

        # Student 1 (Ada) joins BOTH Math 101 and Physics 101
        self.client.post(
            "/login",
            data={"email": self.student1_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        self.client.post("/classrooms", data={"class_code": self.math_code}, follow_redirects=True)
        self.client.post("/classrooms", data={"class_code": self.physics_code}, follow_redirects=True)
        self.client.get("/logout")

        # Student 2 (Grace) joins ONLY Math 101
        self.client.post(
            "/login",
            data={"email": self.student2_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        self.client.post("/classrooms", data={"class_code": self.math_code}, follow_redirects=True)
        self.client.get("/logout")

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass

    def test_enrolled_students_roster_deduplication_and_classrooms(self):
        # Faculty logs in and views /students roster
        self.client.post(
            "/login",
            data={"email": self.faculty_email, "password": "password123", "user_type": "faculty"},
            follow_redirects=True,
        )

        resp = self.client.get("/students")
        self.assertEqual(resp.status_code, 200)

        # In context, students should be exactly 2 distinct students
        # Ada should only appear once as a distinct row even though she is enrolled in 2 classrooms
        page_html = resp.data.decode("utf-8")
        
        # Check Ada is displayed
        self.assertIn("Ada Lovelace", page_html)
        self.assertIn("Grace Hopper", page_html)

        # Count occurrences of student row with data-name for Ada
        ada_rows = page_html.count('data-name="Ada Lovelace"')
        grace_rows = page_html.count('data-name="Grace Hopper"')

        self.assertEqual(ada_rows, 1, "Ada Lovelace must appear only once in the roster table!")
        self.assertEqual(grace_rows, 1, "Grace Hopper must appear only once in the roster table!")

        # Check that both Math 101 and Physics 101 badges are rendered for Ada
        self.assertIn("Math 101", page_html)
        self.assertIn("Physics 101", page_html)
        self.assertIn("All Classrooms (2 Students)", page_html)

    def test_student_view_faculty_chat_box_modal(self):
        # Student logs in and views /students
        self.client.post(
            "/login",
            data={"email": self.student1_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )

        resp = self.client.get("/students")
        self.assertEqual(resp.status_code, 200)
        page_html = resp.data.decode("utf-8")

        # Student should see Course Faculty & Professors section and private chat modal
        self.assertIn("Course Professors &amp; Faculty", page_html)
        self.assertIn("Prof. Prof Gauss", page_html)
        self.assertIn("privateChatModal", page_html)
        self.assertIn("openDirectChat", page_html)

        # Student views /classrooms
        resp_cls = self.client.get("/classrooms")
        self.assertEqual(resp_cls.status_code, 200)
        cls_html = resp_cls.data.decode("utf-8")

        self.assertIn("privateChatModal", cls_html)
        self.assertIn("Message Faculty", cls_html)
        self.assertIn("Open Live Chat Box", cls_html)

        # Student sends message to faculty via /api/chat/send
        conn = self.module.get_db()
        math_class = conn.execute("SELECT * FROM classrooms WHERE class_name='Math 101'").fetchone()
        class_id = math_class["id"]
        conn.close()

        send_res = self.client.post(
            "/api/chat/send",
            data={
                "classroom_id": str(class_id),
                "message": "Hello Professor! I have a question about the calculus problem.",
            },
        )
        self.assertEqual(send_res.status_code, 200)
        send_data = send_res.get_json()
        self.assertEqual(send_data["status"], "ok")
        self.assertEqual(send_data["message"]["message"], "Hello Professor! I have a question about the calculus problem.")

        # Student gets chat history via /api/chat/history
        hist_res = self.client.get(f"/api/chat/history?classroom_id={class_id}")
        self.assertEqual(hist_res.status_code, 200)
        hist_data = hist_res.get_json()
        self.assertEqual(hist_data["status"], "ok")
        self.assertTrue(len(hist_data["messages"]) >= 1)
        self.assertEqual(hist_data["messages"][0]["message"], "Hello Professor! I have a question about the calculus problem.")


if __name__ == "__main__":
    unittest.main()

