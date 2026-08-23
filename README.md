# AI Smart Collaborative Classroom

Flask-based classroom platform with study planning, task tracking, quiz tools, and collaboration features for students and faculty.

## Features

- AI-assisted study schedule generation
- Task and deadline tracking
- Classroom creation and participation
- Faculty dashboard and student progress utilities
- Quiz and study support workflows

## Tech Stack

- Backend: Flask (Python)
- Database: SQLite
- Frontend: HTML, CSS, JavaScript, Bootstrap 5

## Quick Start

1. Clone and enter the project

```bash
git clone https://github.com/adityabshiwarkar9960/AI-Smart-Collaborative-Classroom-.git
cd AI-Smart-Collaborative-Classroom-
```

2. Create and activate a virtual environment

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate
```

3. Install dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

4. Optional environment file

Create a `.env` file if you use external APIs:

```env
SECRET_KEY=your_secret_key
OPENAI_API_KEY=your_openai_api_key
```

5. Run locally

```bash
python app.py
```

Open http://127.0.0.1:5000

## Production (optional)

```bash
gunicorn --bind 0.0.0.0:5000 app:app
```

## Contributing

1. Create a branch
2. Commit your changes
3. Open a pull request
