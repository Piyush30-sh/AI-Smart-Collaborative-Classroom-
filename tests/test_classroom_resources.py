import io
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
    module.init_db()
    return module, db_path, upload_dir


def test_classroom_resources_upload_view_download_delete():
    module, db_path, upload_dir = setup_app_module()
    client = module.app.test_client()

    faculty_email = "prof.albus@university.edu"
    student_email = "harry.potter@university.edu"
    other_student_email = "draco.malfoy@university.edu"

    # Register Faculty & Students
    client.post(
        "/signup",
        data={
            "name": "Prof Albus",
            "email": faculty_email,
            "password": "password123",
            "user_type": "faculty",
        },
        follow_redirects=True,
    )
    client.post(
        "/signup",
        data={
            "name": "Harry Potter",
            "email": student_email,
            "password": "password123",
            "user_type": "student",
        },
        follow_redirects=True,
    )
    client.post(
        "/signup",
        data={
            "name": "Draco Malfoy",
            "email": other_student_email,
            "password": "password123",
            "user_type": "student",
        },
        follow_redirects=True,
    )

    # 1. Faculty logs in and creates classroom
    client.post(
        "/login",
        data={"email": faculty_email, "password": "password123"},
        follow_redirects=True,
    )
    create_resp = client.post(
        "/classrooms",
        data={"class_name": "Defense Against the Dark Arts"},
        follow_redirects=True,
    )
    assert create_resp.status_code == 200

    conn = module.get_db()
    classroom = conn.execute("SELECT * FROM classrooms WHERE class_name='Defense Against the Dark Arts'").fetchone()
    assert classroom is not None
    class_id = classroom["id"]
    class_code = classroom["class_code"]
    conn.close()

    # 2. Faculty uploads a PDF study material
    file_content = b"%PDF-1.4 Mock PDF Content For Classroom Resources Test"
    upload_resp = client.post(
        f"/classrooms/{class_id}/resources/upload",
        data={
            "title": "Lesson 1: Patronus Charm Notes",
            "description": "Essential incantations and theory for positive focus.",
            "resource_type": "file",
            "resource_file": (io.BytesIO(file_content), "patronus_notes.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert upload_resp.status_code == 200
    assert b"Lesson 1: Patronus Charm Notes" in upload_resp.data

    # 3. Faculty uploads a Web Link resource
    link_resp = client.post(
        f"/classrooms/{class_id}/resources/upload",
        data={
            "title": "Ministry of Magic Defense Portal",
            "description": "External reference library guidelines.",
            "resource_type": "link",
            "external_url": "https://hogwarts.edu/defense-portal",
        },
        follow_redirects=True,
    )
    assert link_resp.status_code == 200
    assert b"Ministry of Magic Defense Portal" in link_resp.data

    conn = module.get_db()
    resources = conn.execute("SELECT * FROM classroom_resources WHERE classroom_id=?", (class_id,)).fetchall()
    assert len(resources) == 2
    pdf_resource = [r for r in resources if r["resource_type"] == "pdf"][0]
    link_resource = [r for r in resources if r["resource_type"] == "link"][0]
    conn.close()

    client.get("/logout")

    # 4. Student 1 joins classroom
    client.post(
        "/login",
        data={"email": student_email, "password": "password123"},
        follow_redirects=True,
    )
    join_resp = client.post(
        "/classrooms",
        data={"class_code": class_code},
        follow_redirects=True,
    )
    assert join_resp.status_code == 200

    # View classroom page and check materials appear
    class_view = client.get(f"/classrooms?class_id={class_id}")
    assert class_view.status_code == 200
    assert b"Lesson 1: Patronus Charm Notes" in class_view.data
    assert b"Ministry of Magic Defense Portal" in class_view.data

    # View resources repository page
    resources_page = client.get("/resources")
    assert resources_page.status_code == 200
    assert b"Lesson 1: Patronus Charm Notes" in resources_page.data

    # Download PDF study material
    download_resp = client.get(f"/classrooms/resources/{pdf_resource['id']}/download")
    assert download_resp.status_code == 200
    assert download_resp.data == file_content
    assert "attachment" in download_resp.headers.get("Content-Disposition", "")

    # Preview PDF study material
    preview_resp = client.get(f"/classrooms/resources/{pdf_resource['id']}/preview")
    assert preview_resp.status_code == 200
    assert preview_resp.data == file_content

    # Access Link study material
    link_access = client.get(f"/classrooms/resources/{link_resource['id']}/download")
    assert link_access.status_code in [301, 302]
    assert "https://hogwarts.edu/defense-portal" in link_access.headers.get("Location", "")

    client.get("/logout")

    # 5. Non-enrolled Student 2 attempts to download resource (Unauthorized)
    client.post(
        "/login",
        data={"email": other_student_email, "password": "password123"},
        follow_redirects=True,
    )
    unauth_resp = client.get(f"/classrooms/resources/{pdf_resource['id']}/download", follow_redirects=False)
    assert unauth_resp.status_code in [302, 403]

    client.get("/logout")

    # 6. Faculty logs in and deletes the study material
    client.post(
        "/login",
        data={"email": faculty_email, "password": "password123"},
        follow_redirects=True,
    )
    del_resp = client.post(f"/classrooms/resources/{pdf_resource['id']}/delete", follow_redirects=True)
    assert del_resp.status_code == 200

    conn = module.get_db()
    remaining = conn.execute("SELECT * FROM classroom_resources WHERE id=?", (pdf_resource["id"],)).fetchone()
    assert remaining is None
    conn.close()
