import os
import tempfile
import importlib


def setup_app():
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    upload_dir = tempfile.mkdtemp()
    os.environ["DATABASE_PATH"] = db_path
    os.environ["UPLOAD_FOLDER"] = upload_dir
    module = importlib.import_module("app")
    module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return module.app.test_client()


def test_automatic_role_prediction_on_login():
    client = setup_app()

    faculty_email = "prof.smith@univ.edu"
    student_email = "student.john@univ.edu"

    # Register faculty
    client.post(
        "/signup",
        data={
            "name": "Prof Smith",
            "email": faculty_email,
            "password": "securepassword123",
            "user_type": "faculty",
        },
        follow_redirects=True,
    )

    # Register student
    client.post(
        "/signup",
        data={
            "name": "John Doe",
            "email": student_email,
            "password": "securepassword123",
            "user_type": "student",
        },
        follow_redirects=True,
    )

    # 1. Test faculty login without any user_type field
    faculty_resp = client.post(
        "/login",
        data={"email": faculty_email, "password": "securepassword123"},
        follow_redirects=False,
    )
    # Redirects to faculty dashboard
    assert faculty_resp.status_code == 302
    assert "/faculty-dashboard" in faculty_resp.headers["Location"]

    # Log out
    client.get("/logout")

    # 2. Test student login without any user_type field
    student_resp = client.post(
        "/login",
        data={"email": student_email, "password": "securepassword123"},
        follow_redirects=False,
    )
    # Redirects to student dashboard
    assert student_resp.status_code == 302
    assert "/dashboard" in student_resp.headers["Location"]

    # 3. Test invalid credentials
    client.get("/logout")
    invalid_resp = client.post(
        "/login",
        data={"email": faculty_email, "password": "wrongpassword"},
        follow_redirects=True,
    )
    assert invalid_resp.status_code == 200
    assert b"Invalid email or password" in invalid_resp.data

    # 4. Test login page GET renders cleanly without role selection buttons
    login_page = client.get("/login")
    assert login_page.status_code == 200
    assert b"btnStudent" not in login_page.data
    assert b"btnFaculty" not in login_page.data
    assert b"switchRole" not in login_page.data
