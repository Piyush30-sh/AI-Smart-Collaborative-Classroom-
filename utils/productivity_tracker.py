"""
Utils — Productivity Tracker
==============================
Helper functions for scoring and motivating students.
"""

from datetime import date, timedelta


def calculate_productivity_score(user_id: int, conn) -> float:
    """
    Return a blended productivity score based on study activity and tasks.

    The score favors study consistency, but still rewards task completion.
    This keeps the dashboard meaningful even when the user has no tasks yet.
    """
    today = date.today().strftime("%Y-%m-%d")

    study_hours_today = conn.execute(
        """
        SELECT COALESCE(SUM(hours),0) AS t
        FROM study_sessions
        WHERE user_id=? AND date=?
        """,
        (user_id, today),
    ).fetchone()["t"]

    completed_schedule_hours = conn.execute(
        """
        SELECT COALESCE(SUM(study_hours),0) AS t
        FROM schedules
        WHERE user_id=? AND date=? AND completed=1
        """,
        (user_id, today),
    ).fetchone()["t"]

    planned_today = conn.execute(
        """
        SELECT COALESCE(SUM(study_hours),0) AS t
        FROM schedules
        WHERE user_id=? AND date=?
        """,
        (user_id, today),
    ).fetchone()["t"]

    # Use the larger source for the day so timer-logged sessions and schedule
    # completions do not get double-counted when they refer to the same work.
    daily_study_total = max(float(study_hours_today), float(completed_schedule_hours))
    study_progress = 0.0
    if planned_today > 0:
        study_progress = min((daily_study_total / float(planned_today)) * 100, 100.0)
    elif daily_study_total > 0:
        study_progress = min(daily_study_total * 100, 100.0)

    total_tasks = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (user_id,)
    ).fetchone()["c"]
    task_progress = 0.0
    if total_tasks > 0:
        completed_tasks = conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE user_id=? AND status='Completed'",
            (user_id,),
        ).fetchone()["c"]
        task_progress = (completed_tasks / total_tasks) * 100

    if planned_today > 0 and total_tasks > 0:
        score = (study_progress * 0.7) + (task_progress * 0.3)
    elif planned_today > 0:
        score = study_progress
    elif total_tasks > 0:
        score = task_progress
    else:
        score = daily_study_total * 25

    return round(min(score, 100.0), 1)


def get_weekly_stats(user_id: int, conn) -> dict:
    """
    Return dicts with 'dates' and 'hours' arrays for the last 7 days.
    Used to draw the dashboard line chart.
    """
    dates, hours = [], []
    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).strftime("%Y-%m-%d")
        h = conn.execute(
            """
            SELECT COALESCE(SUM(hours),0) AS t
            FROM study_sessions
            WHERE user_id=? AND date=?
            """,
            (user_id, d),
        ).fetchone()["t"]
        scheduled = conn.execute(
            """
            SELECT COALESCE(SUM(study_hours),0) AS t
            FROM schedules
            WHERE user_id=? AND date=? AND completed=1
            """,
            (user_id, d),
        ).fetchone()["t"]
        h = max(float(h), float(scheduled))
        dates.append(d)
        hours.append(round(h, 2))

    return {"dates": dates, "hours": hours}


def get_study_streak(user_id: int, conn) -> int:
    """
    Count consecutive days (ending today) that have at least one study activity.

    A study activity can be either:
    - a logged study session from the timer, or
    - a completed schedule entry for that day.
    """
    streak  = 0
    current = date.today()

    activity_days = {
        row["activity_date"]
        for row in conn.execute(
            """
            SELECT DISTINCT date AS activity_date
            FROM study_sessions
            WHERE user_id=?
            UNION
            SELECT DISTINCT date AS activity_date
            FROM schedules
            WHERE user_id=? AND completed=1
            """,
            (user_id, user_id),
        ).fetchall()
    }

    while True:
        d = current.strftime("%Y-%m-%d")
        if d in activity_days:
            streak  += 1
            current -= timedelta(days=1)
        else:
            break

    return streak


def get_motivational_message(productivity_score: float) -> str:
    """Return an encouraging message based on the student's score."""
    if productivity_score >= 80:
        return "🌟 Excellent work! You're crushing your goals!"
    if productivity_score >= 60:
        return "💪 Great progress! Keep up the momentum!"
    if productivity_score >= 40:
        return "📚 You're on the right track! Stay consistent!"
    if productivity_score > 0:
        return "🚀 Every step counts! Let's pick up the pace!"
    return "👋 Welcome! Start your learning journey today!"


def get_faculty_weekly_analytics(faculty_id: int, conn) -> dict:
    """
    Return weekly analytics data for faculty dashboard over the past 7 days:
    - dates: list of YYYY-MM-DD
    - labels: short day names (Mon, Tue, etc.)
    - display_dates: formatted day (e.g. Aug 31)
    - assignments: count of student assignment submissions/completions per day
    - quizzes: count of student quiz attempts/submissions per day
    - total_assignments: total assignment completions this week
    - total_quizzes: total quiz attempts this week
    """
    dates = []
    labels = []
    display_dates = []
    assignments = []
    quizzes = []

    today = date.today()
    for i in range(6, -1, -1):
        d_obj = today - timedelta(days=i)
        d_str = d_obj.strftime("%Y-%m-%d")
        dates.append(d_str)
        labels.append(d_obj.strftime("%a"))
        display_dates.append(d_obj.strftime("%b %d"))

    # If faculty has no active classrooms, immediately return reset 0 data
    try:
        active_classrooms = conn.execute(
            "SELECT id FROM classrooms WHERE faculty_id=?", (faculty_id,)
        ).fetchall()
    except Exception:
        active_classrooms = []

    if not active_classrooms:
        return {
            "dates": dates,
            "labels": labels,
            "display_dates": display_dates,
            "assignments": [0] * 7,
            "quizzes": [0] * 7,
            "total_assignments": 0,
            "total_quizzes": 0,
        }

    for d_str in dates:
        # 1. Assignment submissions in active faculty classrooms
        try:
            sub_res = conn.execute(
                """
                SELECT COUNT(DISTINCT sub.id) AS cnt
                FROM classroom_submissions sub
                JOIN classrooms c ON c.id = sub.classroom_id
                WHERE c.faculty_id = ? AND (date(sub.submitted_at) = ? OR strftime('%Y-%m-%d', sub.submitted_at) = ?)
                """,
                (faculty_id, d_str, d_str),
            ).fetchone()
            sub_cnt = int(sub_res["cnt"]) if sub_res and sub_res["cnt"] else 0
        except Exception:
            sub_cnt = 0

        # Also check classroom_assignments completed directly in active classrooms
        try:
            ca_res = conn.execute(
                """
                SELECT COUNT(DISTINCT ca.id) AS cnt
                FROM classroom_assignments ca
                JOIN classrooms c ON c.id = ca.classroom_id
                WHERE c.faculty_id = ? AND (ca.status = 'Completed' OR ca.status = 'Submitted')
                  AND (date(ca.created_at) = ? OR strftime('%Y-%m-%d', ca.created_at) = ?)
                """,
                (faculty_id, d_str, d_str),
            ).fetchone()
            ca_cnt = int(ca_res["cnt"]) if ca_res and ca_res["cnt"] else 0
        except Exception:
            ca_cnt = 0

        assignments.append(max(sub_cnt, ca_cnt))

        # 2. Quiz attempts & submissions in active classrooms
        try:
            qs_res = conn.execute(
                """
                SELECT COUNT(DISTINCT qs.id) AS cnt
                FROM quiz_submissions qs
                JOIN quizzes q ON q.id = qs.quiz_id
                JOIN classrooms c ON c.id = q.classroom_id
                WHERE c.faculty_id = ?
                  AND (date(qs.submitted_at) = ? OR strftime('%Y-%m-%d', qs.submitted_at) = ?)
                """,
                (faculty_id, d_str, d_str),
            ).fetchone()
            qs_cnt = int(qs_res["cnt"]) if qs_res and qs_res["cnt"] else 0
        except Exception:
            qs_cnt = 0

        # Attempts made by currently enrolled students in active classrooms
        try:
            qa_res = conn.execute(
                """
                SELECT COUNT(DISTINCT qa.id) AS cnt
                FROM quiz_attempts qa
                JOIN classroom_members cm ON cm.student_id = qa.user_id
                JOIN classrooms c ON c.id = cm.classroom_id
                WHERE c.faculty_id = ?
                  AND (date(qa.created_at) = ? OR strftime('%Y-%m-%d', qa.created_at) = ?)
                """,
                (faculty_id, d_str, d_str),
            ).fetchone()
            qa_cnt = int(qa_res["cnt"]) if qa_res and qa_res["cnt"] else 0
        except Exception:
            qa_cnt = 0

        quizzes.append(qs_cnt + qa_cnt)

    return {
        "dates": dates,
        "labels": labels,
        "display_dates": display_dates,
        "assignments": assignments,
        "quizzes": quizzes,
        "total_assignments": sum(assignments),
        "total_quizzes": sum(quizzes),
    }


