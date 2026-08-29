import io
import os
import tempfile
import importlib
import unittest


def setup_app_module():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    upload_dir = tempfile.mkdtemp()
    os.environ["DATABASE_PATH"] = db_path
    os.environ["UPLOAD_FOLDER"] = upload_dir
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    module.init_db()
    return module, db_path, upload_dir


class NotificationsTestCase(unittest.TestCase):
    def test_student_and_faculty_realtime_notifications_and_read_dismissals(self):
        module, db_path, upload_dir = setup_app_module()
        client = module.app.test_client()

        faculty_email = "prof.turing@university.edu"
        student_email = "ada.lovelace@university.edu"

        # 1. Sign up faculty and student
        client.post(
            "/signup",
            data={
                "name": "Prof Alan Turing",
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

        # 2. Faculty creates a classroom
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        client.post("/classrooms", data={"class_name": "Theoretical Computer Science"}, follow_redirects=True)

        conn = module.get_db()
        classroom = conn.execute("SELECT * FROM classrooms WHERE class_name='Theoretical Computer Science'").fetchone()
        self.assertIsNotNone(classroom)
        class_id = classroom["id"]
        class_code = classroom["class_code"]
        conn.close()

        # 3. Student joins classroom -> Should trigger enrollment notification for faculty
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)
        client.post("/classrooms", data={"class_code": class_code}, follow_redirects=True)

        # Verify faculty receives "New Student Joined" notification
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        fac_notifs_resp = client.get("/api/notifications")
        self.assertEqual(fac_notifs_resp.status_code, 200)
        fac_data = fac_notifs_resp.get_json()
        self.assertGreater(fac_data["count"], 0)
        join_notifs = [n for n in fac_data["notifications"] if n["type"] == "enrollment"]
        self.assertEqual(len(join_notifs), 1)
        self.assertIn("Ada Lovelace", join_notifs[0]["text"])

        # 4. Faculty publishes an assignment, uploads study notes, and posts a class message
        # a) Assign task
        client.post(
            "/classrooms/assign-task",
            data={
                "classroom_id": str(class_id),
                "target_student_id": "all",
                "task_name": "Turing Machines Analysis",
                "subject": "CompSci",
                "deadline": "2026-11-30",
                "instructions": "Formalize the halting problem proof.",
            },
            follow_redirects=True,
        )

        # b) Upload resource
        res_file = b"%PDF-1.4 Turing Computability lecture slides"
        client.post(
            f"/classrooms/{class_id}/resources/upload",
            data={
                "title": "Turing Machine Foundations",
                "description": "Comprehensive slide deck on formal languages and automata.",
                "resource_type": "file",
                "resource_file": (io.BytesIO(res_file), "turing_notes.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        # c) Post announcement / message to all
        client.post(
            "/classrooms/chat/send",
            data={
                "classroom_id": str(class_id),
                "recipient_student_id": "all",
                "message": "Welcome students! Please review the lecture notes and assignment by Friday.",
            },
            follow_redirects=True,
        )

        # 5. Student logs in -> Verifies receipt of notifications for Task, Material, and Announcement
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)

        student_notifs_resp = client.get("/api/notifications")
        self.assertEqual(student_notifs_resp.status_code, 200)
        student_data = student_notifs_resp.get_json()
        self.assertGreaterEqual(student_data["count"], 3)

        notif_types = [n["type"] for n in student_data["notifications"]]
        self.assertIn("assignment", notif_types)
        self.assertIn("resource", notif_types)
        self.assertIn("announcement", notif_types)

        # Find the assignment notification ID
        assign_notif = next(n for n in student_data["notifications"] if n["type"] == "assignment")
        self.assertIn("Turing Machines Analysis", assign_notif["text"])

        # 6. Student dismisses/reads single notification -> count decreases
        read_resp = client.post(
            "/api/notifications/read",
            json={"id": assign_notif["id"]},
        )
        self.assertEqual(read_resp.status_code, 200)

        updated_student_notifs = client.get("/api/notifications").get_json()
        self.assertEqual(updated_student_notifs["count"], student_data["count"] - 1)
        remaining_ids = [n["id"] for n in updated_student_notifs["notifications"]]
        self.assertNotIn(assign_notif["id"], remaining_ids)

        # 7. Student submits task deliverable
        conn = module.get_db()
        ada_user = conn.execute("SELECT id FROM users WHERE email=?", (student_email,)).fetchone()
        ada_assignment = conn.execute("SELECT id FROM classroom_assignments WHERE student_id=?", (ada_user["id"],)).fetchone()
        conn.close()

        sol_file = b"# Proof of the Halting Problem\ndef halt_checker(): pass\n"
        client.post(
            f"/classrooms/assignments/{ada_assignment['id']}/submit",
            data={
                "submission_text": "Completed the formal reduction proof.",
                "submission_file": (io.BytesIO(sol_file), "halting_proof.py"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        # 8. Faculty logs in -> Verifies notification for Student Task Submission
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)

        fac_notifs_after_sub = client.get("/api/notifications").get_json()
        sub_notifs = [n for n in fac_notifs_after_sub["notifications"] if n["type"] == "submission"]
        self.assertGreaterEqual(len(sub_notifs), 1)
        self.assertIn("Ada Lovelace", sub_notifs[0]["text"])
        self.assertIn("Turing Machines Analysis", sub_notifs[0]["text"])

        # 9. Faculty marks all notifications as read
        all_fac_ids = [n["id"] for n in fac_notifs_after_sub["notifications"]]
        clear_resp = client.post(
            "/api/notifications/read",
            json={"all": True, "ids": all_fac_ids},
        )
        self.assertEqual(clear_resp.status_code, 200)

        cleared_fac_notifs = client.get("/api/notifications").get_json()
        self.assertEqual(cleared_fac_notifs["count"], 0)
        self.assertEqual(len(cleared_fac_notifs["notifications"]), 0)


if __name__ == "__main__":
    unittest.main()
