import io
import os
import tempfile
import importlib
import unittest


class TestRevisionNotes(unittest.TestCase):
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

        self.faculty_email = "prof.turing@university.edu"
        self.student1_email = "ada.lovelace@university.edu"
        self.student2_email = "grace.hopper@university.edu"

        # Register Faculty & 2 Students
        self.client.post(
            "/signup",
            data={
                "name": "Prof Alan Turing",
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

        # Faculty creates a classroom
        self.client.post(
            "/login",
            data={"email": self.faculty_email, "password": "password123", "user_type": "faculty"},
            follow_redirects=True,
        )
        self.client.post(
            "/classrooms",
            data={"class_name": "CS101 Algorithms"},
            follow_redirects=True,
        )
        conn = self.module.get_db()
        classroom = conn.execute("SELECT * FROM classrooms WHERE class_name='CS101 Algorithms'").fetchone()
        self.classroom_id = classroom["id"]
        self.class_code = classroom["class_code"]
        conn.close()
        self.client.get("/logout")

        # Student 1 joins CS101 Algorithms
        self.client.post(
            "/login",
            data={"email": self.student1_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        self.client.post(
            "/classrooms",
            data={"class_code": self.class_code},
            follow_redirects=True,
        )
        self.client.get("/logout")

    def tearDown(self):
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass

    def test_private_vs_classroom_shared_notes(self):
        # 1. Student 1 creates a strictly PRIVATE note (no classroom linked)
        self.client.post(
            "/login",
            data={"email": self.student1_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        self.client.post(
            "/revision-notes/upload",
            data={
                "title": "Ada Private Diary Note",
                "subject_name": "Personal Math",
                "note_type": "Summary",
                "content_type": "manual",
                "note_content": "Private study notes only for Ada.",
                "classroom_id": "",  # Unlinked / private
            },
            follow_redirects=True,
        )

        # 2. Student 1 creates a CLASSROOM-SHARED note linked to CS101 Algorithms
        self.client.post(
            "/revision-notes/upload",
            data={
                "title": "Algorithms Exam Formulas Cheat Sheet",
                "subject_name": "Data Structures",
                "note_type": "Formulas",
                "content_type": "manual",
                "note_content": "Shared formulas for CS101 students:\nBig-O table, Master theorem.",
                "classroom_id": str(self.classroom_id),  # Linked with class
            },
            follow_redirects=True,
        )

        conn = self.module.get_db()
        notes = conn.execute("SELECT * FROM revision_notes ORDER BY id ASC").fetchall()
        private_note = notes[0]
        shared_note = notes[1]
        conn.close()

        self.client.get("/logout")

        # 3. Faculty logs in:
        # Faculty MUST see the shared note for CS101, but MUST NOT see Ada's private note!
        self.client.post(
            "/login",
            data={"email": self.faculty_email, "password": "password123", "user_type": "faculty"},
            follow_redirects=True,
        )
        fac_page = self.client.get("/revision-notes")
        self.assertIn(b"Algorithms Exam Formulas Cheat Sheet", fac_page.data)
        self.assertNotIn(b"Ada Private Diary Note", fac_page.data)

        # Faculty can view the shared note
        fac_view = self.client.get(f"/revision-notes/{shared_note['id']}/view")
        self.assertEqual(fac_view.status_code, 200)
        self.assertEqual(fac_view.get_json()["note"]["title"], "Algorithms Exam Formulas Cheat Sheet")

        # Faculty cannot view Ada's private note
        fac_unauth = self.client.get(f"/revision-notes/{private_note['id']}/view")
        self.assertEqual(fac_unauth.status_code, 404)

        self.client.get("/logout")

        # 4. Student 2 (NOT enrolled in CS101) logs in:
        # Student 2 MUST NOT see Ada's private note AND MUST NOT see the CS101 shared note!
        self.client.post(
            "/login",
            data={"email": self.student2_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        s2_page = self.client.get("/revision-notes")
        self.assertNotIn(b"Ada Private Diary Note", s2_page.data)
        self.assertNotIn(b"Algorithms Exam Formulas Cheat Sheet", s2_page.data)

        # Student 2 cannot access via API or download
        s2_view = self.client.get(f"/revision-notes/{shared_note['id']}/view")
        self.assertEqual(s2_view.status_code, 404)

        s2_dl = self.client.get(f"/revision-notes/{shared_note['id']}/download", follow_redirects=True)
        self.assertIn(b"access denied", s2_dl.data.lower())

        self.client.get("/logout")

        # 5. Student 2 joins CS101 -> Now Student 2 CAN access the shared note!
        self.client.post(
            "/login",
            data={"email": self.student2_email, "password": "password123", "user_type": "student"},
            follow_redirects=True,
        )
        self.client.post(
            "/classrooms",
            data={"class_code": self.class_code},
            follow_redirects=True,
        )
        s2_page_after_join = self.client.get("/revision-notes")
        self.assertIn(b"Algorithms Exam Formulas Cheat Sheet", s2_page_after_join.data)
        self.assertNotIn(b"Ada Private Diary Note", s2_page_after_join.data)

        s2_view_allowed = self.client.get(f"/revision-notes/{shared_note['id']}/view")
        self.assertEqual(s2_view_allowed.status_code, 200)


if __name__ == "__main__":
    unittest.main()
