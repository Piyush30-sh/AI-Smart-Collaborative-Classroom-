import os
import tempfile
import importlib
import json
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


class LiveQuizzesTestCase(unittest.TestCase):
    def test_faculty_create_start_quiz_student_notification_concurrent_attempts_and_results(self):
        module, db_path, upload_dir = setup_app_module()
        client = module.app.test_client()

        faculty_email = "prof.euler@math.edu"
        student1_email = "gauss@math.edu"
        student2_email = "newton@math.edu"

        # 1. Register faculty and 2 students
        client.post(
            "/signup",
            data={
                "name": "Prof Leonhard Euler",
                "email": faculty_email,
                "password": "password123",
                "user_type": "faculty",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Carl Gauss",
                "email": student1_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Isaac Newton",
                "email": student2_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )

        # 2. Faculty creates a classroom
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        client.post("/classrooms", data={"class_name": "Advanced Calculus"}, follow_redirects=True)

        conn = module.get_db()
        classroom = conn.execute("SELECT * FROM classrooms WHERE class_name='Advanced Calculus'").fetchone()
        self.assertIsNotNone(classroom)
        class_id = classroom["id"]
        class_code = classroom["class_code"]
        conn.close()

        # 3. Both students join the classroom
        for s_email in [student1_email, student2_email]:
            client.get("/logout")
            client.post("/login", data={"email": s_email, "password": "password123"}, follow_redirects=True)
            client.post("/classrooms", data={"class_code": class_code}, follow_redirects=True)

        # 4. Faculty creates a manual quiz with 2 questions and starts it live
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)

        quiz_questions = [
            {
                "question": "What is the derivative of x^2?",
                "options": ["2x", "x", "x^3", "2"],
                "answer": "2x",
            },
            {
                "question": "What is the integral of 1/x dx?",
                "options": ["ln|x| + C", "e^x + C", "1/x^2", "x"],
                "answer": "ln|x| + C",
            },
        ]

        create_resp = client.post(
            "/quiz/create",
            json={
                "title": "Calculus Pop Quiz",
                "subject": "Calculus",
                "classroom_id": class_id,
                "duration_minutes": 10,
                "status": "active",
                "questions": quiz_questions,
            },
        )
        self.assertEqual(create_resp.status_code, 200)
        create_data = create_resp.get_json()
        self.assertEqual(create_data["status"], "ok")
        quiz_id = create_data["quiz_id"]

        # 5. Verify Student 1 receives real-time notification about the Live Quiz
        client.get("/logout")
        client.post("/login", data={"email": student1_email, "password": "password123"}, follow_redirects=True)

        notifs_resp = client.get("/api/notifications")
        notifs_data = notifs_resp.get_json()
        quiz_notifs = [n for n in notifs_data["notifications"] if n["id"] == f"livequiz-{quiz_id}"]
        self.assertTrue(len(quiz_notifs) >= 1)
        self.assertIn("Calculus Pop Quiz", quiz_notifs[0]["title"])
        self.assertEqual(quiz_notifs[0]["type"], "quiz")

        # 6. Student 1 attempts the quiz (answers both correctly -> 100%)
        take_view = client.get(f"/quiz/take/{quiz_id}")
        self.assertEqual(take_view.status_code, 200)
        self.assertIn(b"Calculus Pop Quiz", take_view.data)

        sub1_resp = client.post(
            f"/quiz/submit/{quiz_id}",
            json={"answers": {"0": "2x", "1": "ln|x| + C"}},
        )
        self.assertEqual(sub1_resp.status_code, 200)
        sub1_data = sub1_resp.get_json()
        self.assertEqual(sub1_data["score"], 100)
        self.assertEqual(sub1_data["correct_answers"], 2)

        # 7. Student 2 concurrently attempts the quiz (answers 1 correct, 1 wrong -> 50%)
        client.get("/logout")
        client.post("/login", data={"email": student2_email, "password": "password123"}, follow_redirects=True)

        sub2_resp = client.post(
            f"/quiz/submit/{quiz_id}",
            json={"answers": {"0": "2x", "1": "e^x + C"}},
        )
        self.assertEqual(sub2_resp.status_code, 200)
        sub2_data = sub2_resp.get_json()
        self.assertEqual(sub2_data["score"], 50)
        self.assertEqual(sub2_data["correct_answers"], 1)

        # 8. Faculty logs back in and checks notifications & results
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)

        fac_notifs = client.get("/api/notifications").get_json()
        quiz_res_notifs = [n for n in fac_notifs["notifications"] if n["type"] == "quiz_result"]
        self.assertTrue(len(quiz_res_notifs) >= 1)

        # 9. Faculty checks quiz results API
        results_api = client.get(f"/api/quiz/{quiz_id}/results")
        self.assertEqual(results_api.status_code, 200)
        results_data = results_api.get_json()
        self.assertEqual(results_data["total_submissions"], 2)
        self.assertEqual(results_data["highest_score"], 100)
        self.assertEqual(results_data["average_score"], 75.0)
        self.assertEqual(results_data["pass_rate"], 100)

        # 10. Faculty downloads export report
        export_resp = client.get(f"/quiz/export/results/{quiz_id}")
        self.assertEqual(export_resp.status_code, 200)
        self.assertIn(b"QUIZ RESULTS REPORT", export_resp.data)
        self.assertIn(b"Carl Gauss", export_resp.data)
        self.assertIn(b"Isaac Newton", export_resp.data)

    def test_manual_form_quiz_creation_and_faculty_answer_review(self):
        module, db_path, upload_dir = setup_app_module()
        client = module.app.test_client()

        faculty_email = "prof.boole@logic.edu"
        student_email = "shannon@logic.edu"

        # 1. Register faculty and student
        client.post(
            "/signup",
            data={
                "name": "Prof George Boole",
                "email": faculty_email,
                "password": "password123",
                "user_type": "faculty",
            },
            follow_redirects=True,
        )
        client.post(
            "/signup",
            data={
                "name": "Claude Shannon",
                "email": student_email,
                "password": "password123",
                "user_type": "student",
            },
            follow_redirects=True,
        )

        # 2. Faculty creates quiz via standard HTML Form fields
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        form_data = {
            "title": "Boolean Logic Fundamentals",
            "subject": "Logic",
            "duration_minutes": "10",
            "status": "active",
            "q_text_0": "What is 1 AND 0 in Boolean algebra?",
            "q_opt_0_0": "0",
            "q_opt_0_1": "1",
            "q_opt_0_2": "Undefined",
            "q_opt_0_3": "2",
            "q_ans_0": "0",  # Selected radio value = 0 (first option "0")
            "q_text_1": "What is the negation of TRUE?",
            "q_opt_1_0": "MAYBE",
            "q_opt_1_1": "FALSE",
            "q_opt_1_2": "TRUE",
            "q_opt_1_3": "NULL",
            "q_ans_1": "1",  # Selected radio value = 1 (second option "FALSE")
        }
        res = client.post("/quiz/create", data=form_data, follow_redirects=True)
        self.assertEqual(res.status_code, 200)

        conn = module.get_db()
        quiz_row = conn.execute("SELECT * FROM quizzes WHERE title='Boolean Logic Fundamentals'").fetchone()
        self.assertIsNotNone(quiz_row)
        quiz_id = quiz_row["id"]
        parsed_q = json.loads(quiz_row["quiz_data"])
        self.assertEqual(parsed_q[0]["answer"], "0")
        self.assertEqual(parsed_q[1]["answer"], "FALSE")
        conn.close()

        # 3. Student attempts quiz via standard form submission
        client.get("/logout")
        client.post("/login", data={"email": student_email, "password": "password123"}, follow_redirects=True)
        submit_res = client.post(
            f"/quiz/submit/{quiz_id}",
            data={"answer_0": "0", "answer_1": "FALSE"},
            follow_redirects=True,
        )
        self.assertEqual(submit_res.status_code, 200)

        # 4. Faculty reviews the student's submission in Quiz Hub
        client.get("/logout")
        client.post("/login", data={"email": faculty_email, "password": "password123"}, follow_redirects=True)
        view_res = client.get(f"/quiz?view_quiz={quiz_id}")
        self.assertEqual(view_res.status_code, 200)
        self.assertIn(b"Claude Shannon", view_res.data)
        self.assertIn(b"100%", view_res.data)
        self.assertIn(b"studentAnswerModal_", view_res.data)
        self.assertIn(b"What is 1 AND 0 in Boolean algebra?", view_res.data)
        self.assertIn(b"Student's Choice (Correct)", view_res.data)


if __name__ == "__main__":
    unittest.main()
