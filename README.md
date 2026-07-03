AI Student Planner App
=====================

Simple Flask app to help students plan tasks and schedules.

Quick start

- Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
```

- Install dependencies:

```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

- Run locally:

```bash
python app.py
# or for production-like server:
gunicorn --bind 0.0.0.0:5000 app:app
```

Deployment notes

- See `DEPLOY.md` for deploy requirements (Python runtime, prefer-binary, package pins).

Contributing

- Open an issue or submit a PR.
