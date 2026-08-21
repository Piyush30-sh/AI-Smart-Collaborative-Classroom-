import os
import tempfile
import importlib


def setup_app_module():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    upload_dir = tempfile.mkdtemp()
    os.environ["DATABASE_PATH"] = db_path
    os.environ["UPLOAD_FOLDER"] = upload_dir
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return module, db_path


def test_faculty_can_create_classroom_and_student_can_join(tmp_path):
    module, _ = setup_app_module()
    client = module.app.test_client()

    faculty_email = "faculty@example.com"
    student_email = "student@example.com"

    client.post(
        "/signup",
        data={
            "name": "Prof Anna",
            "email": faculty_email,
            "password": "password123",
            "user_type": "faculty",
        },
        follow_redirects=True,
    )
    client.post(
        "/signup",
        data={
            "name": "Sam Student",
            "email": student_email,
            "password": "password123",
            "user_type": "student",
        },
        follow_redirects=True,
    )

    faculty_login = client.post(
        "/login",
        data={"email": faculty_email, "password": "password123", "user_type": "faculty"},
        follow_redirects=True,
    )
    assert faculty_login.status_code == 200

    create_response = client.post(
        "/classrooms",
        data={"class_name": "Biology 101"},
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert b"Biology 101" in create_response.data

    client.get("/logout")

    student_login = client.post(
        "/login",
        data={"email": student_email, "password": "password123", "user_type": "student"},
        follow_redirects=True,
    )
    assert student_login.status_code == 200

    join_response = client.post(
        "/classrooms",
        data={"class_code": "BIO101"},
        follow_redirects=True,
    )
    assert join_response.status_code == 200
    assert b"Biology 101" in join_response.data
