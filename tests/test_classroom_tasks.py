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


class ClassroomTasksTestCase(unittest.TestCase):
    def test_classroom_assignment_submission_and_review_workflow(self):
        module, db_path, upload_dir = setup_app_module()
        client = module.app.test_client()

        faculty_email = "prof.ada@university.edu"
        student_email = "linus.torvalds@university.edu"
        other_student_email = "bill.gates@university.edu"

        # 1. Register Faculty and Students
        client.post(
            "/signup",
            data={
                "name": "Prof Ada",
                "email": faculty_email,
                "password": "password123",
                "user_type": "faculty",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Linus Torvalds",
                "email": student_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Bill Gates",
                "email": other_student_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )

        # 2. Faculty logs in and creates a classroom
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        create_resp = client.post("/classrooms", data={"class_name": "Operating Systems 101"}, follow_redirects=True)
        self.assertEqual(create_resp.status_code, 200)

        conn = module.get_db()
        classroom = conn.execute("SELECT * FROM classrooms WHERE class_name='Operating Systems 101'").fetchone()
        self.assertIsNotNone(classroom)
        class_id = classroom["id"]
        class_code = classroom["class_code"]
        conn.close()

        # 3. Students join the classroom
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)
        join_resp = client.post("/classrooms", data={"class_code": class_code}, follow_redirects=True)
        self.assertEqual(join_resp.status_code, 200)

        client.get("/logout")
        client.post("/login", data={"email": other_student_email, "password": "password123"}, follow_redirects=True)
        client.post("/classrooms", data={"class_code": class_code}, follow_redirects=True)

        # 4. Faculty logs in and assigns a task with instructions and a reference PDF file
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)

        starter_pdf = b"%PDF-1.4 Assignment 1 Starter Rubric and Instructions"
        assign_resp = client.post(
            "/classrooms/assign-task",
            data={
                "classroom_id": str(class_id),
                "target_student_id": "all",
                "task_name": "Lab 1: Process Scheduling Algorithm",
                "subject": "Operating Systems",
                "deadline": "2026-12-31",
                "instructions": "Implement Round Robin and Priority scheduling in Python or C++.",
                "attachment": (io.BytesIO(starter_pdf), "lab1_instructions.pdf"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(assign_resp.status_code, 200)

        conn = module.get_db()
        assignments = conn.execute("SELECT * FROM classroom_assignments WHERE classroom_id=?", (class_id,)).fetchall()
        self.assertEqual(len(assignments), 2)  # Assigned to both students
        linus_user = conn.execute("SELECT id FROM users WHERE email=?", (student_email,)).fetchone()
        linus_assignment = conn.execute(
            "SELECT * FROM classroom_assignments WHERE classroom_id=? AND student_id=?",
            (class_id, linus_user["id"]),
        ).fetchone()
        self.assertIsNotNone(linus_assignment)
        self.assertEqual(linus_assignment["task_name"], "Lab 1: Process Scheduling Algorithm")
        self.assertEqual(linus_assignment["attachment_name"], "lab1_instructions.pdf")
        conn.close()

        # 5. Student (Linus) logs in, views task, and submits work (text notes + solution code file)
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)

        view_resp = client.get(f"/classrooms?class_id={class_id}")
        self.assertEqual(view_resp.status_code, 200)
        self.assertIn(b"Lab 1: Process Scheduling Algorithm", view_resp.data)

        solution_code = b"def round_robin(processes, quantum):\n    print('Scheduling completed!')\n"
        submit_resp = client.post(
            f"/classrooms/assignments/{linus_assignment['id']}/submit",
            data={
                "submission_text": "Completed the Round Robin algorithm with quantum=4. Passes all test cases.",
                "submission_file": (io.BytesIO(solution_code), "scheduler_solution.py"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(submit_resp.status_code, 200)
        self.assertIn(b"submitted successfully", submit_resp.data)

        # Check database state for submission
        conn = module.get_db()
        sub_row = conn.execute(
            "SELECT * FROM classroom_submissions WHERE assignment_id=? AND student_id=?",
            (linus_assignment["id"], linus_user["id"]),
        ).fetchone()
        self.assertIsNotNone(sub_row)
        self.assertEqual(sub_row["status"], "Submitted")
        self.assertIn("Round Robin algorithm", sub_row["submission_text"])
        self.assertEqual(sub_row["file_name"], "scheduler_solution.py")
        sub_id = sub_row["id"]

        # Verify assignment status updated
        updated_assign = conn.execute("SELECT status FROM classroom_assignments WHERE id=?", (linus_assignment["id"],)).fetchone()
        self.assertEqual(updated_assign["status"], "Submitted")
        conn.close()

        # 6. Student downloads their own submission
        down_resp = client.get(f"/classrooms/submissions/{sub_id}/download")
        self.assertEqual(down_resp.status_code, 200)
        self.assertIn(b"round_robin", down_resp.data)

        # 7. Student previews their submission
        prev_resp = client.get(f"/classrooms/submissions/{sub_id}/preview")
        self.assertEqual(prev_resp.status_code, 200)
        self.assertIn(b"round_robin", prev_resp.data)

        # 8. Faculty reviews submission, downloads solution file, and grades/provides feedback
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)

        # Faculty downloads student submission
        fac_down_resp = client.get(f"/classrooms/submissions/{sub_id}/download")
        self.assertEqual(fac_down_resp.status_code, 200)
        self.assertIn(b"round_robin", fac_down_resp.data)

        # Faculty grades submission
        grade_resp = client.post(
            f"/classrooms/submissions/{sub_id}/grade",
            data={
                "grade": "A+",
                "feedback": "Outstanding implementation! Clean logic and optimal time complexity.",
                "status": "Completed",
            },
            follow_redirects=True,
        )
        self.assertEqual(grade_resp.status_code, 200)
        self.assertIn(b"feedback saved", grade_resp.data)

        # Verify grade in DB
        conn = module.get_db()
        graded_sub = conn.execute("SELECT * FROM classroom_submissions WHERE id=?", (sub_id,)).fetchone()
        self.assertEqual(graded_sub["grade"], "A+")
        self.assertEqual(graded_sub["status"], "Completed")
        self.assertIn("Outstanding implementation", graded_sub["feedback"])
        conn.close()

        # 9. Student checks classroom and sees evaluation
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)
        student_view_resp = client.get(f"/classrooms?class_id={class_id}&tab=tab-student-tasks")
        self.assertEqual(student_view_resp.status_code, 200)
        self.assertIn(b"A+", student_view_resp.data)

        # 10. Security check: other student cannot download Linus's submission
        client.get("/logout")
        client.post("/login", data={"email": other_student_email, "password": "password123"}, follow_redirects=True)
        unauthorized_resp = client.get(f"/classrooms/submissions/{sub_id}/download", follow_redirects=True)
        self.assertIn(b"do not have permission", unauthorized_resp.data)


if __name__ == "__main__":
    unittest.main()
