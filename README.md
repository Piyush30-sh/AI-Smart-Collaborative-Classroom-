# AI Smart Collaborative Classroom 🎓

An intelligent Study Planner and Productivity Coach designed to help students plan their tasks, generate schedules, track productivity, and collaborate in classrooms. The platform leverages AI to create optimized study plans and provides tools for educators to manage classrooms effectively.

## 🚀 Features

*   **AI-Powered Schedule Generation**: Generate personalized study schedules based on tasks, priorities, and deadlines.
*   **Productivity Tracking**: Monitor study streaks, calculate productivity scores, and view weekly statistics to stay on track.
*   **Classroom Management**: Create and join classrooms for collaborative learning.
*   **Faculty Dashboard**: Empower educators with tools to manage student progress, assignments, and class analytics.
*   **Automated Quizzes**: Generate flashcards and quizzes from uploaded study materials (e.g., PDFs).
*   **Motivational Coaching**: Receive tailored motivational messages and feedback based on study patterns.

## 🛠️ Technology Stack

*   **Backend**: Python, Flask
*   **Database**: SQLite
*   **AI & Machine Learning**: OpenAI API, Scikit-Learn, NumPy, Pandas
*   **Document Processing**: PyPDF
*   **Frontend**: HTML, CSS, JavaScript 

## 📦 Installation & Setup

Follow these steps to set up the project locally:

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/adityabshiwarkar9960/AI-Smart-Collaborative-Classroom-.git
    cd AI-Smart-Collaborative-Classroom-
    ```

2.  **Create and activate a virtual environment**:
    ```bash
    python -m venv .venv
    
    # On Windows:
    .venv\Scripts\activate
    
    # On macOS/Linux:
    source .venv/bin/activate
    ```

3.  **Install dependencies**:
    ```bash
    pip install --upgrade pip setuptools wheel
    pip install -r requirements.txt
    ```

4.  **Set up Environment Variables**:
    Create a `.env` file in the root directory and add required API keys or secrets (if applicable):
    ```env
    SECRET_KEY=your_secret_key_here
    OPENAI_API_KEY=your_openai_api_key_here
    ```

5.  **Run the application locally**:
    ```bash
    python app.py
    ```
    The app will be available at `http://127.0.0.1:5000`.

## 🌐 Deployment

For a production-like server environment, use Gunicorn:
```bash
gunicorn --bind 0.0.0.0:5000 app:app
```

## 🤝 Contributing

We welcome contributions! Please follow these steps:
1.  Fork the repository.
2.  Create a new branch (`git checkout -b feature/YourFeatureName`).
3.  Commit your changes (`git commit -m 'Add some feature'`).
4.  Push to the branch (`git push origin feature/YourFeatureName`).
5.  Open a Pull Request.
