"""
AI Smart Colaborative Learning Platform
======================================
Main Flask application entry point.

Run with:  python app.py
Visit:     http://127.0.0.1:5000
"""

from flask import (Flask, render_template, request, redirect,
                   url_for, session, jsonify, flash, make_response,
                   send_from_directory, abort)
import sqlite3
import os
import json
import io
import random
import re
import string
import zipfile
import tempfile
from datetime import datetime, date, timedelta
from functools import wraps
from collections import defaultdict, Counter
from werkzeug.security import generate_password_hash, check_password_hash
import pandas as pd
from pypdf import PdfReader

from ai_engine.schedule_generator import generate_schedule, get_priority_breakdown
from utils.productivity_tracker import (
    calculate_productivity_score,
    get_weekly_stats,
    get_study_streak,
    get_motivational_message,
)
import socket
import webbrowser
import secrets
import smtplib
from email.message import EmailMessage
from werkzeug.utils import secure_filename
import uuid
import time


BASE_DIR = os.path.dirname(__file__)


def _is_hosted_runtime():
    """Return True when running on a hosted platform with ephemeral storage."""
    return any(
        os.environ.get(name)
        for name in ("RENDER", "RENDER_SERVICE_ID", "VERCEL", "VERCEL_ENV")
    )


def get_database_path():
    """Return a writable database path for the current environment."""
    override_path = os.environ.get("DATABASE_PATH")
    if override_path:
        return override_path
    if _is_hosted_runtime():
        return os.path.join(tempfile.gettempdir(), "ai_student_planner.db")
    return os.path.join(BASE_DIR, "database.db")


def get_upload_folder():
    """Return the avatar upload folder for the current environment."""
    override_path = os.environ.get("UPLOAD_FOLDER")
    if override_path:
        return override_path
    if _is_hosted_runtime():
        return os.path.join(tempfile.gettempdir(), "uploads", "avatars")
    return os.path.join(BASE_DIR, "static", "uploads", "avatars")


def generate_class_code(class_name: str) -> str:
    """Create a simple classroom code from a class name."""
    letters = re.sub(r"[^A-Za-z]", "", (class_name or "").upper())
    digits = re.sub(r"[^0-9]", "", (class_name or ""))

    if letters and digits:
        return (letters[:3] + digits[:3]).upper()[:6]
    if letters:
        return (letters[:6]).upper()

    return "CLASS" + "".join(secrets.choice(string.digits) for _ in range(3))


def generate_unique_class_code(class_name: str, conn, max_attempts: int = 8) -> str:
    """Generate a short, human-friendly class code and ensure it's unique in DB.

    Tries a few deterministic variants from the class name then falls back to
    random alphanumeric codes. Uses the provided DB connection `conn` to
    check uniqueness.
    """
    base = generate_class_code(class_name) or "CLASS"
    # normalized form of the class name to avoid producing an identical code
    norm_name = re.sub(r"[^A-Z0-9]", "", (class_name or "").upper())
    charset = string.ascii_uppercase + string.digits

    for attempt in range(max_attempts):
        if attempt == 0:
            candidate = base[:8]
        else:
            suffix = "".join(secrets.choice(charset) for _ in range(3))
            candidate = (base[:5] + suffix).upper()[:8]

        exists = conn.execute("SELECT 1 FROM classrooms WHERE class_code=?", (candidate,)).fetchone()
        # Ensure the candidate is not identical to a normalized class name
        cand_norm = re.sub(r"[^A-Z0-9]", "", candidate.upper())
        if not exists and cand_norm != norm_name:
            return candidate

    # As a last resort, use a slightly longer random token to guarantee uniqueness
    while True:
        candidate = "C" + secrets.token_hex(3).upper()
        cand_norm = re.sub(r"[^A-Z0-9]", "", candidate.upper())
        exists = conn.execute("SELECT 1 FROM classrooms WHERE class_code=?", (candidate,)).fetchone()
        if not exists and cand_norm != norm_name:
            return candidate


# Password reset tokens table creation
def ensure_password_resets_table():
    # Use direct sqlite connection here because get_db() may not be defined yet
    db_path = get_database_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_resets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            expires_at DATETIME NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

ensure_password_resets_table()

# ── App configuration ────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "study-planner-secret-key-2024")
DATABASE = get_database_path()
UPLOAD_FOLDER = get_upload_folder()
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


@app.template_filter("days_until")
def days_until_filter(date_val):
    """Return number of days remaining until the given date (negative if overdue)."""
    if not date_val:
        return 0
    try:
        if isinstance(date_val, datetime):
            dt = date_val.date()
        elif isinstance(date_val, date):
            dt = date_val
        elif isinstance(date_val, str):
            clean_str = str(date_val).strip()[:10]
            dt = datetime.strptime(clean_str, "%Y-%m-%d").date()
        else:
            return 0
        return (dt - date.today()).days
    except Exception:
        return 0


@app.template_filter("filesize_format")
def filesize_format_filter(size_bytes):
    """Format file size in bytes to human-readable string (KB, MB, GB)."""
    if not size_bytes or size_bytes <= 0:
        return ""
    try:
        size = float(size_bytes)
        if size < 1024:
            return f"{int(size)} B"
        elif size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        elif size < 1024 * 1024 * 1024:
            return f"{size / (1024 * 1024):.1f} MB"
        else:
            return f"{size / (1024 * 1024 * 1024):.1f} GB"
    except Exception:
        return ""



# ── Database helpers ─────────────────────────────────────────────────────────
def get_db():
    """Open a database connection and enable row-factory mode."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def safe_get(row, key, default=None):
    """Safely get a value from a mapping-like or sqlite3.Row object.

    sqlite3.Row doesn't implement `.get()`, so try `.get()` first then fallback
    to index access. Returns `default` on any error.
    """
    if row is None:
        return default
    try:
        return row.get(key, default)
    except Exception:
        try:
            return row[key]
        except Exception:
            return default


QUIZ_MAX_QUESTIONS = 5
QUIZ_MIN_SENTENCE_WORDS = 8
QUIZ_MAX_SENTENCE_WORDS = 45
QUIZ_STOPWORDS = {
    "about", "after", "again", "also", "because", "been", "being", "between",
    "could", "doing", "during", "each", "from", "have", "having", "into", "most",
    "other", "over", "such", "than", "that", "their", "there", "these", "they",
    "this", "those", "through", "under", "very", "were", "what", "when", "where",
    "which", "while", "with", "would", "your", "student", "study", "subject",
    "chapter", "section", "page", "pages", "figure", "table", "example",
    "also", "can", "may", "might", "must", "should", "will", "shall", "using",
    "used", "use", "because", "however", "therefore", "thus", "then", "than",
    "into", "onto", "over", "under", "about", "across", "within", "without"
}

QUIZ_CUE_PATTERNS = [
    r"\b(is|are|was|were)\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bmeans\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\brefers?\s+to\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bdefined\s+as\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bconsists?\s+of\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bincludes?\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\brepresents?\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bresults?\s+in\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
    r"\bleads?\s+to\s+(?:the\s+)?(.{4,120}?)\s*(?:[.;,]|$)",
]

QUIZ_GENERIC_CHOICES = [
    "concept", "method", "process", "principle", "factor", "system", "result", "effect",
    "feature", "property", "idea", "stage", "cause", "result", "example", "solution",
]

QUIZ_GENERIC_PHRASES = {
    "main idea",
    "important concepts",
    "important concept",
    "these tasks",
    "this pdf",
    "sample quiz pdf",
    "overview",
    "core concepts",
    "revision notes",
    "real-world applications",
    "use this pdf",
    "regular sentences",
    "multiple-choice questions",
    "the main idea",
}

QUIZ_GENERIC_WORDS = {
    "important", "concept", "concepts", "overview", "notes", "applications",
    "review", "sample", "question", "questions", "sentence", "sentences",
    "tasks", "idea", "ideas", "pdf",
}

QUIZ_BAD_ANSWER_WORDS = {
    "perform", "learns", "learn", "uses", "used", "using", "helps", "allows",
    "combines", "supports", "creates", "creating", "made", "make", "making",
    "field", "human", "normally", "requiring", "that", "these", "this", "those",
    "carry", "needs", "include", "includes", "contains", "contain", "produce",
    "produces", "real", "world", "tasks",
}

QUIZ_DEFINITION_CUES = (
    "is", "are", "was", "were", "means", "refers to", "defined as",
    "consists of", "includes", "represents", "results in", "leads to",
)


def _extract_pdf_text(uploaded_file) -> str:
    uploaded_file.stream.seek(0)
    reader = PdfReader(uploaded_file.stream)
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n".join(pages).strip()


def _normalize_quiz_text(text: str) -> str:
    normalized = re.sub(r"\r\n?", "\n", text or "")
    normalized = re.sub(r"\n{2,}", ". ", normalized)
    normalized = re.sub(r"[\t ]+", " ", normalized)
    normalized = re.sub(r"\s*\n\s*", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _split_quiz_sentences(text: str):
    sentences = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        cleaned = sentence.strip().strip("•-–—")
        word_count = len(cleaned.split())
        if QUIZ_MIN_SENTENCE_WORDS <= word_count <= QUIZ_MAX_SENTENCE_WORDS:
            sentences.append(cleaned)
    return sentences


def _looks_like_quiz_heading(line: str) -> bool:
    cleaned = re.sub(r"\s+", " ", line or "").strip()
    if not cleaned:
        return True
    if re.search(r"[.!?]$", cleaned):
        return False

    words = cleaned.split()
    word_count = len(words)
    if word_count > 10:
        return False

    if re.search(r"\b(is|are|was|were|means|refers?|defined|consists?|includes?|results?|leads?|helps|allows|supports|combines|uses|can|may|might|should)\b", cleaned, re.IGNORECASE):
        return False

    alpha_words = [word for word in words if re.search(r"[A-Za-z]", word)]
    if not alpha_words:
        return True

    title_like = sum(1 for word in alpha_words if word[:1].isupper()) / len(alpha_words) >= 0.6
    short_and_title_like = word_count <= 6 and title_like
    title_with_colon = word_count <= 8 and ":" in cleaned and title_like
    return short_and_title_like or title_with_colon


def _quiz_content_text(text: str) -> str:
    body_lines = []
    for raw_line in re.split(r"\r?\n", text or ""):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line or _looks_like_quiz_heading(line):
            continue
        body_lines.append(line)
    return " ".join(body_lines)


def _clean_quiz_phrase(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", text or "").strip(" \t\n\r.,;:!?()[]{}-")
    cleaned = re.sub(r"^(?:the|a|an)\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:and|or|of|to|in|for|with|on|at|by)\s*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _quiz_words(text: str):
    return [
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z\-']+", text or "")
        if len(token) >= 4 and token.lower() not in QUIZ_STOPWORDS
    ]


def _phrase_candidates_for_sentence(sentence: str):
    words = _quiz_words(sentence)
    candidates = []
    for size in (3, 2, 1):
        for index in range(0, len(words) - size + 1):
            candidate = " ".join(words[index:index + size]).strip()
            if len(candidate.split()) == 1 and candidate in QUIZ_STOPWORDS:
                continue
            candidates.append(candidate)
    return candidates


def _extract_definition_pair(sentence: str):
    patterns = [
        r"\b(?P<subject>.+?)\s+(?:is|are|was|were)\s+(?:the|a|an|one of the|part of the|type of|form of|kind of)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+means\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+refers?\s+to\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+defined\s+as\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+consists?\s+of\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+includes?\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+represents?\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+results?\s+in\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
        r"\b(?P<subject>.+?)\s+leads?\s+to\s+(?:the|a|an)?\s*(?P<predicate>[^.;:]{4,140}?)\s*(?:[.;,]|$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, sentence, re.IGNORECASE)
        if not match:
            continue
        subject = _clean_quiz_phrase(re.split(r"[,;:]", match.group("subject"), maxsplit=1)[0])
        predicate = _clean_quiz_phrase(match.group("predicate"))
        if subject and predicate and _is_valid_quiz_answer(subject):
            return subject, predicate
    return None, None


def _find_original_phrase(sentence: str, phrase: str):
    if not phrase:
        return ""
    match = re.search(rf"\b{re.escape(phrase)}\b", sentence, re.IGNORECASE)
    return match.group(0).strip() if match else phrase


def _is_valid_quiz_answer(answer: str) -> bool:
    cleaned = _clean_quiz_phrase(answer).lower()
    if not cleaned:
        return False
    if cleaned in QUIZ_GENERIC_PHRASES:
        return False
    if cleaned.startswith(("this ", "that ", "these ", "those ", "important ", "use this ", "sample ")):
        return False

    words = cleaned.split()
    if len(words) == 1:
        return len(cleaned) >= 4 and cleaned not in QUIZ_GENERIC_WORDS and cleaned not in QUIZ_STOPWORDS
    if len(words) > 4:
        return False
    if any(word in QUIZ_BAD_ANSWER_WORDS for word in words):
        return False
    return True


def _is_meta_quiz_sentence(sentence: str) -> bool:
    lowered = re.sub(r"\s+", " ", sentence or "").strip().lower()
    if not lowered:
        return True
    return any(
        phrase in lowered
        for phrase in (
            "main idea",
            "important concepts",
            "use this pdf",
            "sample quiz pdf",
            "multiple-choice questions",
            "contains regular sentences",
            "regular sentences",
            "test the quiz generator",
        )
    )


def _extract_salient_phrase(sentence: str, phrase_frequency: Counter, token_frequency: Counter):
    best_phrase = None
    best_score = -1

    words = _quiz_words(sentence)
    if not words:
        return None

    for candidate in _phrase_candidates_for_sentence(sentence):
        if not _is_valid_quiz_answer(candidate):
            continue
        candidate_words = candidate.split()
        if not candidate_words:
            continue
        score = phrase_frequency.get(candidate, 0) * 3
        score += sum(token_frequency.get(word, 0) for word in candidate_words)
        score += len(candidate_words) * 2
        if score > best_score:
            best_phrase = candidate
            best_score = score

    if best_phrase:
        candidate = _find_original_phrase(sentence, best_phrase)
        candidate_words = candidate.split()
        if len(candidate_words) > 1:
            has_title_case = any(word[:1].isupper() or word.isupper() for word in candidate_words)
            if not has_title_case and phrase_frequency.get(best_phrase, 0) <= 1:
                best_phrase = None
            else:
                return candidate if _is_valid_quiz_answer(candidate) else None
        else:
            return candidate if _is_valid_quiz_answer(candidate) else None

    fallback_words = [word for word in words if word not in QUIZ_STOPWORDS]
    if not fallback_words:
        return None
    fallback_words.sort(key=lambda word: (token_frequency.get(word, 0), len(word)), reverse=True)
    candidate = _find_original_phrase(sentence, fallback_words[0])
    return candidate if _is_valid_quiz_answer(candidate) else None


def _sentence_score(sentence: str) -> int:
    score = len(sentence.split())
    if re.search(r"\b(is|are|was|were|means|refers?|defined|consists?|includes?|results?|leads?|represents?)\b", sentence, re.IGNORECASE):
        score += 10
    if re.search(r"\b(?:important|important|main|key|primary|central|significant|because|therefore)\b", sentence, re.IGNORECASE):
        score += 4
    return score


def _extract_answer_phrase(sentence: str, term_frequency: Counter) -> str | None:
    best_phrase = None
    best_score = -1

    for pattern in QUIZ_CUE_PATTERNS:
        for match in re.finditer(pattern, sentence, re.IGNORECASE):
            phrase_group = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
            if not phrase_group:
                continue
            phrase = _clean_quiz_phrase(phrase_group)
            words = phrase.split()
            if not (2 <= len(words) <= 12):
                continue
            phrase_score = sum(term_frequency.get(word.lower(), 0) for word in words)
            phrase_score += len(words)
            if phrase_score > best_score:
                best_phrase = phrase
                best_score = phrase_score

    if best_phrase:
        return best_phrase

    tokens = [
        token for token in re.findall(r"[A-Za-z][A-Za-z\-']+", sentence)
        if len(token) >= 5 and token.lower() not in QUIZ_STOPWORDS
    ]
    if not tokens:
        return None

    tokens.sort(key=lambda token: (term_frequency.get(token.lower(), 0), len(token)), reverse=True)
    fallback = _clean_quiz_phrase(tokens[0])
    return fallback if fallback else None


def _build_question_prompt(sentence: str, answer: str, kind: str, clue: str = ""):
    if kind == "definition" and clue:
        return f"Which term is described by this statement? {clue}"
    if kind == "definition":
        return f"Which term is described by this statement? {sentence}"
    if kind == "topic":
        return f"What is the main concept discussed here? {sentence}"
    word_count = len(answer.split())
    if word_count <= 4 and len(answer) <= 28:
        return f"Fill in the blank: {_mask_phrase(sentence, answer)}"
    return f"Which option best matches this idea? {sentence}"


def _mask_phrase(sentence: str, phrase: str) -> str:
    if not phrase:
        return sentence
    masked = re.sub(
        rf"\b{re.escape(phrase)}\b",
        "_____",
        sentence,
        count=1,
        flags=re.IGNORECASE,
    )
    return masked if masked != sentence else sentence.replace(phrase, "_____", 1)


def _is_good_distractor(candidate: str, answer: str) -> bool:
    candidate_norm = candidate.lower()
    answer_norm = answer.lower()
    if candidate_norm == answer_norm:
        return False
    if len(candidate_norm) < 3 or len(answer_norm) < 3:
        return False
    candidate_words = set(candidate_norm.split())
    answer_words = set(answer_norm.split())
    if candidate_words & answer_words:
        return False
    return True


def _quiz_prompt_for(sentence: str, answer: str) -> str:
    word_count = len(answer.split())
    if word_count <= 3 and len(answer) <= 24:
        return f"Fill the blank: {_mask_phrase(sentence, answer)}"
    return f"Best answer: {sentence}"


def _build_distractors(answer: str, answer_pool, term_frequency: Counter):
    answer_words = answer.lower().split()
    answer_size = len(answer_words)
    candidates = []

    for candidate in answer_pool:
        if not _is_good_distractor(candidate, answer):
            continue
        candidate_size = len(candidate.split())
        size_penalty = abs(candidate_size - answer_size)
        frequency_score = term_frequency.get(candidate.lower(), 0)
        candidates.append((size_penalty, -frequency_score, candidate))

    candidates.sort()
    distractors = []
    for _, __, candidate in candidates:
        if candidate not in distractors:
            distractors.append(candidate)
        if len(distractors) == 3:
            break

    if len(distractors) < 3:
        for fallback in QUIZ_GENERIC_CHOICES:
            if fallback not in distractors and _is_good_distractor(fallback, answer):
                distractors.append(fallback)
            if len(distractors) == 3:
                break

    return distractors


def _build_quiz_questions(text: str, limit: int = QUIZ_MAX_QUESTIONS):
    normalized = _normalize_quiz_text(_quiz_content_text(text))
    if not normalized:
        return []

    sentences = _split_quiz_sentences(normalized)
    tokens = _quiz_words(normalized)
    if not sentences or not tokens:
        return []

    frequency = Counter(tokens)
    phrase_frequency = Counter()
    for sentence in sentences:
        phrase_frequency.update(_phrase_candidates_for_sentence(sentence))

    candidate_rows = []
    for sentence in sentences:
        if _is_meta_quiz_sentence(sentence):
            continue

        subject_text, predicate_text = _extract_definition_pair(sentence)
        if subject_text and predicate_text:
            subject_bonus = 8 if any(part[:1].isupper() or part.isupper() for part in subject_text.split()) else 0
            candidate_rows.append({
                "sentence": sentence,
                "answer": subject_text,
                "question": _build_question_prompt(sentence, subject_text, "definition", clue=predicate_text),
                "kind": "definition",
                "score": _sentence_score(sentence) + (len(subject_text.split()) * 5) + min(len(predicate_text.split()), 6) + subject_bonus,
            })
            continue

        answer_text = _extract_salient_phrase(sentence, phrase_frequency, frequency)
        if not answer_text:
            continue

        answer_bonus = 8 if any(part[:1].isupper() or part.isupper() for part in answer_text.split()) else 0
        if len(answer_text.split()) == 1 and answer_text.lower() == answer_text:
            answer_bonus -= 3

        candidate_rows.append({
            "sentence": sentence,
            "answer": answer_text,
            "question": _build_question_prompt(sentence, answer_text, "topic"),
            "kind": "topic",
            "score": _sentence_score(sentence) + (len(answer_text.split()) * 5) + answer_bonus,
        })

    candidate_rows.sort(key=lambda row: row["score"], reverse=True)

    questions = []
    used_answers = set()
    answer_pool = [row["answer"] for row in candidate_rows]

    for row in candidate_rows:
        answer_text = row["answer"]
        answer_key = answer_text.lower()
        if answer_key in used_answers:
            continue

        distractors = _build_distractors(answer_text, answer_pool, frequency)
        options = [answer_text] + distractors
        options = list(dict.fromkeys(options))
        if len(options) < 4:
            continue

        random.shuffle(options)
        questions.append({
            "question": row["question"],
            "options": options,
            "answer": answer_text,
        })
        used_answers.add(answer_key)

        if len(questions) >= limit:
            break

    if questions:
        return questions

    # Fallback: if no pattern-based questions were found, use the highest-value terms.
    unique_terms = []
    for term, _ in frequency.most_common():
        if term not in unique_terms:
            unique_terms.append(term)

    for term in unique_terms:
        sentence = next((s for s in sentences if re.search(rf"\b{re.escape(term)}\b", s, re.IGNORECASE)), None)
        if not sentence:
            continue
        masked_sentence = _mask_phrase(sentence, term)
        distractors = _build_distractors(term, unique_terms, frequency)
        options = [term] + distractors
        options = list(dict.fromkeys(options))
        if len(options) < 4:
            continue
        random.shuffle(options)
        questions.append({
            "question": _quiz_prompt_for(sentence, term),
            "options": options,
            "answer": term,
        })
        if len(questions) >= limit:
            break

    return questions


def _fetch_quiz_attempts(user_id: int, limit: int = 10):
    conn = get_db()
    attempts = conn.execute(
        """
        SELECT id, user_id, source_name, total_questions, correct_answers, score,
               quiz_data, quiz_results, created_at
        FROM quiz_attempts
        WHERE user_id = ?
        ORDER BY datetime(created_at) DESC, id DESC
        LIMIT ?
        """,
        (user_id, limit),
    ).fetchall()
    conn.close()
    return attempts


def _build_quiz_txt_content(title: str, quiz_questions, source_name: str = "", attempt=None, quiz_results=None):
    """Build quiz content as plain text format."""
    lines = []
    lines.append("=" * 80)
    lines.append(title.center(80))
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    if source_name:
        lines.append(f"Source: {source_name}")
    
    if attempt:
        lines.append(f"Score: {attempt['score']}%")
        lines.append(f"Correct Answers: {attempt['correct_answers']}/{attempt['total_questions']}")
    
    lines.append("")
    lines.append("-" * 80)
    lines.append("")
    
    for index, question in enumerate(quiz_questions, start=1):
        lines.append(f"Question {index}")
        lines.append("-" * 40)
        lines.append(question.get("question", ""))
        lines.append("")
        lines.append("Options:")
        
        options = question.get("options", [])
        for opt_index, option in enumerate(options, start=1):
            lines.append(f"  {chr(64 + opt_index)}. {option}")
        
        if quiz_results:
            result = quiz_results[index - 1] if index - 1 < len(quiz_results) else {}
            selected = result.get("selected", "No answer selected")
            correct = result.get("correct", question.get("answer", ""))
            status = "✓ CORRECT" if result.get("is_correct") else "✗ INCORRECT"
            lines.append("")
            lines.append(f"Your Answer: {selected}")
            lines.append(f"Correct Answer: {correct}")
            lines.append(f"Status: {status}")
        
        lines.append("")
        lines.append("")
    
    return "\n".join(lines)


def _quiz_txt_response(filename: str, content: str):
    """Return TXT file as response."""
    response = make_response(content.encode('utf-8'))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def init_db():
    """Create all tables if they do not yet exist."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            email      TEXT    UNIQUE NOT NULL,
            password   TEXT    NOT NULL,
            user_type  TEXT    NOT NULL DEFAULT 'student',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS subjects (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            subject_name   TEXT    NOT NULL,
            difficulty     TEXT    NOT NULL,
            exam_date      DATE    NOT NULL,
            required_hours REAL    NOT NULL,
            daily_hours    REAL    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            task_name  TEXT    NOT NULL,
            subject    TEXT    NOT NULL,
            deadline   DATE    NOT NULL,
            status     TEXT    DEFAULT 'Pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS study_sessions (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            subject TEXT    NOT NULL,
            hours   REAL    NOT NULL,
            date    DATE    NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS schedules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            date        DATE    NOT NULL,
            subject     TEXT    NOT NULL,
            study_hours REAL    NOT NULL,
            scheduled_time TEXT,
            completed   INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS quiz_attempts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id         INTEGER NOT NULL,
            source_name     TEXT    DEFAULT '',
            total_questions INTEGER NOT NULL,
            correct_answers INTEGER NOT NULL,
            score           INTEGER NOT NULL,
            quiz_data       TEXT    NOT NULL,
            quiz_results    TEXT    NOT NULL,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classrooms (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id  INTEGER NOT NULL,
            class_name  TEXT    NOT NULL,
            class_code  TEXT    NOT NULL UNIQUE,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classroom_members (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            classroom_id  INTEGER NOT NULL,
            student_id    INTEGER NOT NULL,
            joined_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(classroom_id, student_id),
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
            FOREIGN KEY (student_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classroom_assignments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            classroom_id    INTEGER NOT NULL,
            faculty_id      INTEGER NOT NULL,
            student_id      INTEGER NOT NULL,
            task_name       TEXT    NOT NULL,
            subject         TEXT    NOT NULL,
            deadline        DATE    NOT NULL,
            instructions    TEXT    DEFAULT '',
            status          TEXT    DEFAULT 'Assigned',
            borrowed        INTEGER DEFAULT 0,
            started         INTEGER DEFAULT 0,
            borrowed_task_id INTEGER DEFAULT NULL,
            attachment_path TEXT DEFAULT '',
            attachment_name TEXT DEFAULT '',
            attachment_mime TEXT DEFAULT '',
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
            FOREIGN KEY (faculty_id) REFERENCES users(id),
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (borrowed_task_id) REFERENCES tasks(id)
        );

        CREATE TABLE IF NOT EXISTS classroom_messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            classroom_id  INTEGER NOT NULL,
            faculty_id    INTEGER NOT NULL,
            student_id    INTEGER NOT NULL,
            sender_id     INTEGER NOT NULL,
            message       TEXT    NOT NULL,
            read_by_faculty INTEGER DEFAULT 0,
            read_by_student INTEGER DEFAULT 0,
            attachment_path TEXT DEFAULT '',
            attachment_name TEXT DEFAULT '',
            attachment_mime TEXT DEFAULT '',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
            FOREIGN KEY (faculty_id) REFERENCES users(id),
            FOREIGN KEY (student_id) REFERENCES users(id),
            FOREIGN KEY (sender_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classroom_resources (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            classroom_id  INTEGER NOT NULL,
            uploader_id   INTEGER NOT NULL,
            title         TEXT    NOT NULL,
            description   TEXT    DEFAULT '',
            resource_type TEXT    DEFAULT 'file',
            file_path     TEXT    DEFAULT '',
            file_name     TEXT    DEFAULT '',
            file_size     INTEGER DEFAULT 0,
            file_mime     TEXT    DEFAULT '',
            external_url  TEXT    DEFAULT '',
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id),
            FOREIGN KEY (uploader_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS classroom_submissions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id   INTEGER NOT NULL,
            classroom_id    INTEGER NOT NULL,
            student_id      INTEGER NOT NULL,
            submission_text TEXT    DEFAULT '',
            file_path       TEXT    DEFAULT '',
            file_name       TEXT    DEFAULT '',
            file_size       INTEGER DEFAULT 0,
            file_mime       TEXT    DEFAULT '',
            submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status          TEXT    DEFAULT 'Submitted',
            grade           TEXT    DEFAULT '',
            feedback        TEXT    DEFAULT '',
            graded_at       TIMESTAMP NULL,
            FOREIGN KEY (assignment_id) REFERENCES classroom_assignments(id) ON DELETE CASCADE,
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS notification_reads (
            user_id     INTEGER NOT NULL,
            notif_id    TEXT NOT NULL,
            read_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, notif_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS revision_notes (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            uploader_id     INTEGER NOT NULL,
            uploader_role   TEXT    NOT NULL DEFAULT 'student',
            subject_name    TEXT    NOT NULL,
            title           TEXT    NOT NULL,
            description     TEXT    DEFAULT '',
            content_type    TEXT    NOT NULL DEFAULT 'file',
            note_content    TEXT    DEFAULT '',
            note_type       TEXT    DEFAULT 'Lecture Notes',
            file_path       TEXT    DEFAULT '',
            file_name       TEXT    DEFAULT '',
            file_size       INTEGER DEFAULT 0,
            file_mime       TEXT    DEFAULT '',
            classroom_id    INTEGER DEFAULT NULL,
            downloads_count INTEGER DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (uploader_id) REFERENCES users(id),
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS quizzes (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            faculty_id       INTEGER NOT NULL,
            classroom_id     INTEGER DEFAULT NULL,
            title            TEXT    NOT NULL,
            subject          TEXT    DEFAULT '',
            description      TEXT    DEFAULT '',
            duration_minutes INTEGER DEFAULT 0,
            status           TEXT    DEFAULT 'draft',
            quiz_data        TEXT    NOT NULL,
            started_at       TIMESTAMP DEFAULT NULL,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (faculty_id) REFERENCES users(id),
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS quiz_submissions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            quiz_id         INTEGER NOT NULL,
            student_id      INTEGER NOT NULL,
            student_name    TEXT    NOT NULL,
            student_email   TEXT    DEFAULT '',
            classroom_id    INTEGER DEFAULT NULL,
            score           INTEGER NOT NULL,
            correct_answers INTEGER NOT NULL,
            total_questions INTEGER NOT NULL,
            answers_json    TEXT    NOT NULL,
            results_json    TEXT    NOT NULL,
            submitted_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (quiz_id) REFERENCES quizzes(id) ON DELETE CASCADE,
            FOREIGN KEY (student_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (classroom_id) REFERENCES classrooms(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_revnotes_subject ON revision_notes(subject_name);
        CREATE INDEX IF NOT EXISTS idx_revnotes_role ON revision_notes(uploader_role);
        CREATE INDEX IF NOT EXISTS idx_quizzes_faculty ON quizzes(faculty_id);
        CREATE INDEX IF NOT EXISTS idx_quizzes_class ON quizzes(classroom_id);
        CREATE INDEX IF NOT EXISTS idx_quizzes_status ON quizzes(status);
        CREATE INDEX IF NOT EXISTS idx_quiz_subs_quiz ON quiz_submissions(quiz_id);
        CREATE INDEX IF NOT EXISTS idx_quiz_subs_student ON quiz_submissions(student_id);
    """)
    conn.commit()
    conn.close()
    # Ensure users table has user_type and daily_hours_allowed columns for per-user settings
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'user_type' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN user_type TEXT NOT NULL DEFAULT 'student'")
        conn.commit()
    if 'daily_hours_allowed' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN daily_hours_allowed REAL DEFAULT 6.0")
        conn.commit()
    # Add timer-related columns if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'timer_focus' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timer_focus INTEGER DEFAULT 25")
    if 'timer_short' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timer_short INTEGER DEFAULT 5")
    if 'timer_long' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timer_long INTEGER DEFAULT 15")
    if 'timer_sessions_before_long' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timer_sessions_before_long INTEGER DEFAULT 4")
    if 'timer_expanded' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN timer_expanded INTEGER DEFAULT 0")
    conn.commit()
    conn.close()
    # Add profile fields if missing
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if 'class_name' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN class_name TEXT DEFAULT ''")
    if 'age' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN age INTEGER DEFAULT NULL")
    if 'avatar' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT ''")
    if 'receive_emails' not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN receive_emails INTEGER DEFAULT 1")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cols = [r[1] for r in conn.execute("PRAGMA table_info(schedules)").fetchall()]
    if 'scheduled_time' not in cols:
        conn.execute("ALTER TABLE schedules ADD COLUMN scheduled_time TEXT")
        conn.commit()
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "user_type" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN user_type TEXT NOT NULL DEFAULT 'student'")
            conn.commit()
    conn.close()

    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    msg_cols = [r[1] for r in conn.execute("PRAGMA table_info(classroom_messages)").fetchall()]
    if 'read_by_faculty' not in msg_cols:
        conn.execute("ALTER TABLE classroom_messages ADD COLUMN read_by_faculty INTEGER DEFAULT 0")
    if 'read_by_student' not in msg_cols:
        conn.execute("ALTER TABLE classroom_messages ADD COLUMN read_by_student INTEGER DEFAULT 0")
    if 'attachment_path' not in msg_cols:
        conn.execute("ALTER TABLE classroom_messages ADD COLUMN attachment_path TEXT DEFAULT ''")
    if 'attachment_name' not in msg_cols:
        conn.execute("ALTER TABLE classroom_messages ADD COLUMN attachment_name TEXT DEFAULT ''")
    if 'attachment_mime' not in msg_cols:
        conn.execute("ALTER TABLE classroom_messages ADD COLUMN attachment_mime TEXT DEFAULT ''")
    # Ensure classroom_assignments has attachment columns
    assign_cols = [r[1] for r in conn.execute("PRAGMA table_info(classroom_assignments)").fetchall()]
    if 'attachment_path' not in assign_cols:
        conn.execute("ALTER TABLE classroom_assignments ADD COLUMN attachment_path TEXT DEFAULT ''")
    if 'attachment_name' not in assign_cols:
        conn.execute("ALTER TABLE classroom_assignments ADD COLUMN attachment_name TEXT DEFAULT ''")
    if 'attachment_mime' not in assign_cols:
        conn.execute("ALTER TABLE classroom_assignments ADD COLUMN attachment_mime TEXT DEFAULT ''")
    # Ensure tasks table supports attachments
    task_cols = [r[1] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()]
    if 'attachment_path' not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN attachment_path TEXT DEFAULT ''")
    if 'attachment_name' not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN attachment_name TEXT DEFAULT ''")
    if 'attachment_mime' not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN attachment_mime TEXT DEFAULT ''")
    # Ensure classroom_submissions table columns
    sub_cols = [r[1] for r in conn.execute("PRAGMA table_info(classroom_submissions)").fetchall()]
    if sub_cols:
        if 'grade' not in sub_cols:
            conn.execute("ALTER TABLE classroom_submissions ADD COLUMN grade TEXT DEFAULT ''")
        if 'feedback' not in sub_cols:
            conn.execute("ALTER TABLE classroom_submissions ADD COLUMN feedback TEXT DEFAULT ''")
        if 'graded_at' not in sub_cols:
            conn.execute("ALTER TABLE classroom_submissions ADD COLUMN graded_at TIMESTAMP NULL")

    # Ensure revision_notes table columns
    rev_cols = [r[1] for r in conn.execute("PRAGMA table_info(revision_notes)").fetchall()]
    if rev_cols:
        if 'content_type' not in rev_cols:
            conn.execute("ALTER TABLE revision_notes ADD COLUMN content_type TEXT NOT NULL DEFAULT 'file'")
        if 'note_content' not in rev_cols:
            conn.execute("ALTER TABLE revision_notes ADD COLUMN note_content TEXT DEFAULT ''")

    conn.commit()
    conn.close()


# Initialize database tables when the module is imported so serverless hosts
# have the schema available without relying on __main__ startup code.
init_db()


def auto_reschedule_missed_sessions(user_id: int, conn) -> int:
    """
    Auto-reschedule missed schedule entries (date < today and not completed).

    Rules:
    - Keep each session's original hours.
    - Move to earliest future day with remaining capacity (max 8h/day).
    - Never place on/after the subject's exam date when known.
    Returns number of sessions moved.
    """
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    missed = conn.execute(
        """SELECT id, subject, study_hours, date
           FROM schedules
           WHERE user_id=? AND completed=0 AND date < ?
           ORDER BY date, id""",
        (user_id, today_str),
    ).fetchall()

    if not missed:
        return 0

    subject_exams = {
        row["subject_name"]: datetime.strptime(row["exam_date"], "%Y-%m-%d").date()
        for row in conn.execute(
            "SELECT subject_name, exam_date FROM subjects WHERE user_id= ?", (user_id,)
        ).fetchall()
    }

    moved = 0
    for row in missed:
        session_hours = float(row["study_hours"])
        subject_name = row["subject"]
        exam_date = subject_exams.get(subject_name, today + timedelta(days=60))

        # Search upcoming days; avoid scheduling on/after exam date.
        for offset in range(0, 60):
            candidate = today + timedelta(days=offset)
            if candidate >= exam_date:
                break

            cstr = candidate.strftime("%Y-%m-%d")
            planned = conn.execute(
                """SELECT COALESCE(SUM(study_hours),0) AS t
                   FROM schedules
                   WHERE user_id=? AND date=? AND id != ?""",
                (user_id, cstr, row["id"]),
            ).fetchone()["t"]

            if float(planned) + session_hours <= 8.0:
                conn.execute(
                    "UPDATE schedules SET date=? WHERE id=? AND user_id=?",
                    (cstr, row["id"], user_id),
                )
                moved += 1
                break

    return moved


def regenerate_user_schedule(user_id: int, conn) -> int:
    """Rebuild schedules for a user from current subjects and return row count."""
    subs = conn.execute(
        "SELECT * FROM subjects WHERE user_id=? ORDER BY exam_date", (user_id,)
    ).fetchall()
    subjects_data = [dict(s) for s in subs]

    conn.execute("DELETE FROM schedules WHERE user_id=?", (user_id,))
    if not subjects_data:
        return 0

    schedule = generate_schedule(subjects_data)
    for entry in schedule:
        conn.execute(
            "INSERT INTO schedules (user_id, date, subject, study_hours) VALUES (?,?,?,?)",
            (user_id, entry["date"], entry["subject"], entry["hours"]),
        )
    return len(schedule)


# ── Auth decorator ────────────────────────────────────────────────────────────
def login_required(f):
    """Redirect unauthenticated requests to the login page."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please login to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Context Processor ────────────────────────────────────────────────────────


@app.context_processor
def inject_user_timer_settings():
    """Provide per-user timer and daily-limit settings to all templates."""
    if 'user_id' not in session:
        return {}
    try:
        conn = get_db()
        row = conn.execute(
            """
            SELECT COALESCE(daily_hours_allowed,6.0) AS daily,
                   COALESCE(timer_focus,25) AS timer_focus,
                   COALESCE(timer_short,5) AS timer_short,
                   COALESCE(timer_long,15) AS timer_long,
                   COALESCE(timer_sessions_before_long,4) AS timer_sessions_before_long
            FROM users WHERE id=?
            """,
            (session['user_id'],),
        ).fetchone()
        conn.close()
        if not row:
            return {}
        return {
            'daily_limit': round(float(row['daily']), 1),
            'timer_focus': int(row['timer_focus']),
            'timer_short': int(row['timer_short']),
            'timer_long': int(row['timer_long']),
            'timer_sessions_before_long': int(row['timer_sessions_before_long']),
            'timer_expanded': int(row['timer_expanded']) if row['timer_expanded'] is not None else 0,
        }
    except Exception:
        return {}


@app.route('/api/timer-layout', methods=['POST'])
@login_required
def save_timer_layout():
    """Persist timer widget layout state for the current user."""
    data = request.get_json(silent=True) or {}
    expanded = 1 if data.get('expanded') else 0
    conn = get_db()
    try:
        conn.execute('UPDATE users SET timer_expanded=? WHERE id=?', (expanded, session['user_id']))
        conn.commit()
        return jsonify({'status': 'ok', 'expanded': bool(expanded)})
    except sqlite3.Error as e:
        conn.rollback()
        return jsonify({'status': 'error', 'message': str(e)}), 500
    finally:
        conn.close()


# ── Authentication routes ─────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        if session.get("user_type") == "faculty":
            return redirect(url_for("faculty_dashboard"))
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        conn = get_db()
        user = conn.execute(
            "SELECT * FROM users WHERE lower(email) = lower(?)", (email,)
        ).fetchone()
        conn.close()

        if user and check_password_hash(user["password"], password):
            stored_type = safe_get(user, "user_type", "student").strip().lower()
            session["user_id"]     = user["id"]
            session["user_name"]   = user["name"]
            session["user_type"]   = stored_type
            session["user_avatar"] = (user["avatar"] if "avatar" in user.keys() else "") or ""
            flash(f"Welcome back, {user['name']}! 🎉", "success")
            if stored_type == "faculty":
                return redirect(url_for("faculty_dashboard"))
            return redirect(url_for("dashboard"))
        else:
            flash("Invalid email or password.", "danger")

    return render_template("login.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "user_id" in session:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        name      = request.form.get("name", "").strip()
        email     = request.form.get("email", "").strip()
        password  = request.form.get("password", "")
        user_type = request.form.get("user_type", "student").strip().lower()
        if user_type not in ("student", "faculty"):
            user_type = "student"

        if len(password) < 6:
            flash("Password must be at least 6 characters.", "danger")
            return render_template("signup.html")

        hashed = generate_password_hash(password)
        try:
            conn = get_db()
            conn.execute(
                "INSERT INTO users (name, email, password, user_type) VALUES (?, ?, ?, ?)",
                (name, email, hashed, user_type),
            )
            # Ensure daily_hours_allowed is set to default if column exists
            try:
                conn.execute(
                    "UPDATE users SET daily_hours_allowed = COALESCE(daily_hours_allowed, ?) WHERE email=?",
                    (6.0, email),
                )
            except Exception:
                pass
            conn.commit()
            conn.close()
            flash("Account created! Please log in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Email already registered.", "danger")

    return render_template("signup.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ── Public landing page ───────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")


def _build_dashboard_context(uid, user_type=None):
    today = date.today().strftime("%Y-%m-%d")
    conn  = get_db()

    # Feature: auto-reschedule missed study sessions
    rescheduled_count = auto_reschedule_missed_sessions(uid, conn)
    if rescheduled_count > 0:
        conn.commit()

    today_schedule = conn.execute(
        "SELECT * FROM schedules WHERE user_id=? AND date=? ORDER BY subject",
        (uid, today),
    ).fetchall()

    pending_tasks = conn.execute(
        "SELECT * FROM tasks WHERE user_id=? AND status='Pending' ORDER BY deadline LIMIT 5",
        (uid,),
    ).fetchall()

    total_tasks = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (uid,)
    ).fetchone()["c"]

    completed_count = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE user_id=? AND status='Completed'", (uid,)
    ).fetchone()["c"]

    today_hours = conn.execute(
        "SELECT COALESCE(SUM(hours),0) AS t FROM study_sessions WHERE user_id=? AND date=?",
        (uid, today),
    ).fetchone()["t"]

    completed_schedule_hours_today = conn.execute(
        "SELECT COALESCE(SUM(study_hours),0) AS t FROM schedules WHERE user_id=? AND date=? AND completed=1",
        (uid, today),
    ).fetchone()["t"]

    today_hours = max(float(today_hours), float(completed_schedule_hours_today))

    # User's daily available hours (fallback to 6.0)
    try:
        row = conn.execute("SELECT daily_hours_allowed FROM users WHERE id=?", (uid,)).fetchone()
        daily_limit = float(row["daily_hours_allowed"]) if row and row["daily_hours_allowed"] is not None else 6.0
    except Exception:
        daily_limit = 6.0

    planned_today = conn.execute(
        "SELECT COALESCE(SUM(study_hours),0) AS t FROM schedules WHERE user_id=? AND date=?",
        (uid, today),
    ).fetchone()["t"]

    productivity = calculate_productivity_score(uid, conn)
    weekly_data  = get_weekly_stats(uid, conn)
    streak       = get_study_streak(uid, conn)
    motivation   = get_motivational_message(productivity)

    # ── Upcoming deadlines across personal tasks, classroom assignments, & exams
    upcoming = []

    # 1. Personal pending tasks
    task_rows = conn.execute(
        """SELECT id, task_name, subject, deadline, status
           FROM tasks
           WHERE user_id=? AND status='Pending'
           ORDER BY deadline ASC""",
        (uid,),
    ).fetchall()

    for t in task_rows:
        upcoming.append({
            "id": t["id"],
            "task_name": t["task_name"],
            "subject": t["subject"],
            "deadline": str(t["deadline"]),
            "status": t["status"],
            "type": "Task",
            "link": "/tasks",
        })

    if user_type != "faculty":
        # 2. Active classroom assignments for students
        try:
            ca_rows = conn.execute(
                """SELECT ca.id, ca.classroom_id, ca.task_name, ca.subject, ca.deadline, ca.status,
                          ca.borrowed, ca.borrowed_task_id, c.class_name
                   FROM classroom_assignments ca
                   JOIN classrooms c ON c.id = ca.classroom_id
                   WHERE ca.student_id=? AND ca.status != 'Completed'
                   ORDER BY ca.deadline ASC""",
                (uid,),
            ).fetchall()

            for ca in ca_rows:
                # If borrowed into personal tasks and already listed in pending tasks, don't duplicate
                if ca["borrowed_task_id"] and any(t["id"] == ca["borrowed_task_id"] for t in task_rows):
                    continue
                upcoming.append({
                    "id": ca["id"],
                    "task_name": ca["task_name"],
                    "subject": f"{ca['class_name']} ({ca['subject']})" if ca["class_name"] else ca["subject"],
                    "deadline": str(ca["deadline"]),
                    "status": ca["status"],
                    "type": "Assignment",
                    "link": f"/classrooms?class_id={ca['classroom_id']}",
                })
        except Exception:
            pass

        # 3. Upcoming subject exams
        try:
            today_date = date.today()
            exam_rows = conn.execute(
                """SELECT id, subject_name, exam_date
                   FROM subjects
                   WHERE user_id=? AND exam_date IS NOT NULL AND exam_date != ''
                   ORDER BY exam_date ASC""",
                (uid,),
            ).fetchall()

            for ex in exam_rows:
                try:
                    ex_date = datetime.strptime(str(ex["exam_date"]).strip()[:10], "%Y-%m-%d").date()
                    if (ex_date - today_date).days >= -1:
                        upcoming.append({
                            "id": ex["id"],
                            "task_name": f"{ex['subject_name']} Exam",
                            "subject": ex["subject_name"],
                            "deadline": str(ex["exam_date"]),
                            "status": "Scheduled",
                            "type": "Exam",
                            "link": "/subjects",
                        })
                except Exception:
                    pass
        except Exception:
            pass

    # Sort all upcoming items by deadline date ASC (overdue/earliest first)
    def parse_item_deadline(item):
        try:
            return datetime.strptime(str(item.get("deadline", "")).strip()[:10], "%Y-%m-%d").date()
        except Exception:
            return date.max

    upcoming.sort(key=parse_item_deadline)

    if user_type == "faculty":
        classroom_summary = conn.execute(
            """SELECT c.id, c.class_name, c.class_code, COUNT(cm.student_id) AS student_count
               FROM classrooms c
               LEFT JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE c.faculty_id=?
               GROUP BY c.id
               ORDER BY c.created_at DESC
               LIMIT 3""",
            (uid,),
        ).fetchall()
    else:
        classroom_summary = conn.execute(
            """SELECT c.id, c.class_name, c.class_code, u.name AS faculty_name
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               JOIN users u ON u.id = c.faculty_id
               WHERE cm.student_id=?
               ORDER BY c.created_at DESC
               LIMIT 3""",
            (uid,),
        ).fetchall()

    conn.close()

    return {
        "today_schedule": today_schedule,
        "pending_tasks": pending_tasks,
        "completed_count": completed_count,
        "total_tasks": total_tasks,
        "productivity": productivity,
        "today_hours": round(today_hours, 1),
        "weekly_data": weekly_data,
        "streak": streak,
        "motivation": motivation,
        "upcoming": upcoming,
        "rescheduled_count": rescheduled_count,
        "daily_limit": round(daily_limit, 1),
        "planned_today": round(planned_today, 1),
        "classroom_summary": classroom_summary,
    }


# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/dashboard")
@login_required
def dashboard():
    if session.get("user_type") == "faculty":
        return redirect(url_for("faculty_dashboard"))
    context = _build_dashboard_context(session["user_id"], session.get("user_type"))
    return render_template("dashboard.html", is_faculty=False, **context)


@app.route("/faculty-dashboard")
@login_required
def faculty_dashboard():
    if session.get("user_type") != "faculty":
        return redirect(url_for("dashboard"))

    uid = session["user_id"]
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    # Fetch faculty's created classrooms with member counts
    classrooms = conn.execute(
        """SELECT c.id, c.class_name, c.class_code, c.created_at,
                  COUNT(DISTINCT cm.student_id) AS student_count
           FROM classrooms c
           LEFT JOIN classroom_members cm ON cm.classroom_id = c.id
           WHERE c.faculty_id = ?
           GROUP BY c.id
           ORDER BY c.created_at DESC""",
        (uid,),
    ).fetchall()

    # Total distinct enrolled students across all classrooms
    student_count_row = conn.execute(
        """SELECT COUNT(DISTINCT cm.student_id) AS total
           FROM classrooms c
           JOIN classroom_members cm ON cm.classroom_id = c.id
           WHERE c.faculty_id = ?""",
        (uid,),
    ).fetchone()
    total_students = student_count_row["total"] if student_count_row else 0

    # Recent assignments issued by this faculty
    assignments = conn.execute(
        """SELECT ca.id, ca.task_name, ca.subject, ca.deadline, ca.instructions, ca.status,
                  ca.borrowed, ca.started, ca.created_at, ca.attachment_name,
                  u.name AS student_name, c.class_name
           FROM classroom_assignments ca
           JOIN classrooms c ON c.id = ca.classroom_id
           JOIN users u ON u.id = ca.student_id
           WHERE ca.faculty_id = ?
           ORDER BY ca.created_at DESC
           LIMIT 10""",
        (uid,),
    ).fetchall()

    # Engagement rate estimate based on assignment and member activity
    engagement_score = 92 if total_students > 0 else 85

    conn.close()

    return render_template(
        "faculty_dashboard.html",
        classrooms=classrooms,
        total_students=total_students,
        assignments=assignments,
        engagement_score=engagement_score,
        today_str=today_str,
    )


@app.route("/students")
@login_required
def students():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()

    if user_type == "faculty":
        classrooms = conn.execute(
            "SELECT id, class_name, class_code FROM classrooms WHERE faculty_id=? ORDER BY class_name",
            (uid,),
        ).fetchall()
        raw_students = conn.execute(
            """SELECT u.id, u.name, u.email, u.class_name AS class_name_profile,
                      c.id AS classroom_id, c.class_name, c.faculty_id, cm.joined_at
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               JOIN users u ON u.id = cm.student_id
               WHERE c.faculty_id = ?
               ORDER BY u.name COLLATE NOCASE, c.class_name""",
            (uid,),
        ).fetchall()
    else:
        classrooms = conn.execute(
            """SELECT c.id, c.class_name, c.class_code
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE cm.student_id=?""",
            (uid,),
        ).fetchall()
        raw_students = conn.execute(
            """SELECT u.id, u.name, u.email, u.class_name AS class_name_profile,
                      c.id AS classroom_id, c.class_name, c.faculty_id, f.name AS faculty_name, cm.joined_at
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               JOIN users u ON u.id = cm.student_id
               JOIN users f ON f.id = c.faculty_id
               WHERE c.id IN (SELECT classroom_id FROM classroom_members WHERE student_id=?)
               ORDER BY u.name COLLATE NOCASE, c.class_name""",
            (uid,),
        ).fetchall()

    conn.close()

    # Deduplicate students so each unique student appears only once
    students_dict = {}
    classroom_counts = Counter()
    faculty_dict = {}

    for r in raw_students:
        s_id = r["id"]
        c_id = r["classroom_id"]
        c_name = r["class_name"]
        classroom_counts[c_id] += 1

        if user_type != "faculty" and "faculty_id" in r.keys() and r["faculty_id"]:
            f_id = r["faculty_id"]
            if f_id not in faculty_dict:
                faculty_dict[f_id] = {
                    "id": f_id,
                    "name": r["faculty_name"],
                    "classrooms": [],
                }
            if not any(c["id"] == c_id for c in faculty_dict[f_id]["classrooms"]):
                faculty_dict[f_id]["classrooms"].append({
                    "id": c_id,
                    "name": c_name,
                })

        if s_id not in students_dict:
            students_dict[s_id] = {
                "id": r["id"],
                "name": r["name"],
                "email": r["email"],
                "class_name_profile": r["class_name_profile"],
                "joined_at": r["joined_at"],
                "classrooms": [],
                "classroom_ids": [],
            }
        students_dict[s_id]["classrooms"].append({
            "id": c_id,
            "name": c_name,
            "joined_at": r["joined_at"],
        })
        students_dict[s_id]["classroom_ids"].append(str(c_id))

    unique_students = list(students_dict.values())
    faculty_list = list(faculty_dict.values())

    return render_template(
        "students.html",
        students=unique_students,
        faculty_list=faculty_list,
        classrooms=classrooms,
        classroom_counts=classroom_counts,
        user_type=user_type,
    )



@app.route("/api/chat/history")
@login_required
def api_chat_history():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    classroom_id_raw = request.args.get("classroom_id", "").strip()
    student_id_raw = request.args.get("student_id", "").strip()

    if not classroom_id_raw.isdigit():
        return jsonify({"status": "error", "message": "Invalid classroom ID."}), 400

    classroom_id = int(classroom_id_raw)
    conn = get_db()

    if user_type == "faculty":
        classroom = conn.execute(
            "SELECT id, faculty_id FROM classrooms WHERE id=? AND faculty_id=?",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            return jsonify({"status": "error", "message": "Classroom not found or unauthorized."}), 403

        if not student_id_raw.isdigit():
            conn.close()
            return jsonify({"status": "error", "message": "Student ID required."}), 400

        faculty_id = uid
        student_id = int(student_id_raw)

        conn.execute(
            """UPDATE classroom_messages
               SET read_by_faculty = 1
               WHERE classroom_id = ? AND faculty_id = ? AND student_id = ? AND sender_id != ?""",
            (classroom_id, faculty_id, student_id, uid),
        )
        conn.commit()
    else:
        classroom = conn.execute(
            """SELECT c.id, c.faculty_id
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE c.id=? AND cm.student_id=?""",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            return jsonify({"status": "error", "message": "Classroom not found or unauthorized."}), 403

        faculty_id = classroom["faculty_id"]
        student_id = uid

        conn.execute(
            """UPDATE classroom_messages
               SET read_by_student = 1
               WHERE classroom_id = ? AND faculty_id = ? AND student_id = ? AND sender_id != ?""",
            (classroom_id, faculty_id, student_id, uid),
        )
        conn.commit()

    messages = conn.execute(
        """SELECT m.id, m.sender_id, m.message, m.created_at,
                  COALESCE(m.attachment_path, '') AS attachment_path,
                  COALESCE(m.attachment_name, '') AS attachment_name,
                  u.name AS sender_name
           FROM classroom_messages m
           JOIN users u ON u.id = m.sender_id
           WHERE m.classroom_id = ? AND m.faculty_id = ? AND m.student_id = ?
           ORDER BY m.created_at ASC""",
        (classroom_id, faculty_id, student_id),
    ).fetchall()

    result = []
    for m in messages:
        result.append({
            "id": m["id"],
            "sender_id": m["sender_id"],
            "sender_name": m["sender_name"],
            "message": m["message"],
            "created_at": m["created_at"],
            "attachment_path": m["attachment_path"],
            "attachment_name": m["attachment_name"],
            "is_mine": (m["sender_id"] == uid),
        })

    conn.close()
    return jsonify({"status": "ok", "messages": result})


@app.route("/api/chat/send", methods=["POST"])
@login_required
def api_chat_send():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    classroom_id_raw = request.form.get("classroom_id", "").strip()
    student_id_raw = request.form.get("student_id", "").strip()
    message = request.form.get("message", "").strip()

    if not classroom_id_raw.isdigit() or not message:
        return jsonify({"status": "error", "message": "Classroom ID and message are required."}), 400

    classroom_id = int(classroom_id_raw)
    conn = get_db()

    attachment = request.files.get("attachment")
    attachment_path = ""
    attachment_name = ""
    attachment_mime = ""
    if attachment and attachment.filename:
        save_dir = os.path.join(UPLOAD_FOLDER, "classroom_messages")
        os.makedirs(save_dir, exist_ok=True)
        fname = secure_filename(attachment.filename)
        unique = f"{int(datetime.utcnow().timestamp())}_{secrets.token_hex(6)}_{fname}"
        dest = os.path.join(save_dir, unique)
        attachment.save(dest)
        attachment_path = os.path.join("classroom_messages", unique)
        attachment_name = attachment.filename
        attachment_mime = attachment.mimetype or ""

    if user_type == "faculty":
        classroom = conn.execute(
            "SELECT id, faculty_id FROM classrooms WHERE id=? AND faculty_id=?",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            return jsonify({"status": "error", "message": "Classroom not found or unauthorized."}), 403

        if not student_id_raw.isdigit():
            conn.close()
            return jsonify({"status": "error", "message": "Student ID required."}), 400

        faculty_id = uid
        student_id = int(student_id_raw)

        cursor = conn.execute(
            """INSERT INTO classroom_messages
               (classroom_id, faculty_id, student_id, sender_id, message, read_by_faculty, read_by_student,
                attachment_path, attachment_name, attachment_mime)
               VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, ?)""",
            (classroom_id, faculty_id, student_id, uid, message, attachment_path, attachment_name, attachment_mime),
        )
    else:
        classroom = conn.execute(
            """SELECT c.id, c.faculty_id
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE c.id=? AND cm.student_id=?""",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            return jsonify({"status": "error", "message": "Classroom not found or unauthorized."}), 403

        faculty_id = classroom["faculty_id"]
        student_id = uid
        cursor = conn.execute(
            """INSERT INTO classroom_messages
               (classroom_id, faculty_id, student_id, sender_id, message, read_by_faculty, read_by_student,
                attachment_path, attachment_name, attachment_mime)
               VALUES (?, ?, ?, ?, ?, 0, 1, ?, ?, ?)""",
            (classroom_id, faculty_id, student_id, uid, message, attachment_path, attachment_name, attachment_mime),
        )

    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()

    return jsonify({
        "status": "ok",
        "message": {
            "id": msg_id,
            "sender_id": uid,
            "sender_name": session.get("user_name", "You"),
            "message": message,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "attachment_path": attachment_path,
            "attachment_name": attachment_name,
            "is_mine": True,
        }
    })


@app.route("/resources")
@login_required
def resources():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()

    if user_type == "faculty":
        study_materials = conn.execute(
            """SELECT r.id, r.classroom_id, r.title, r.description, r.resource_type,
                      r.file_path, r.file_name, r.file_size, r.file_mime, r.external_url,
                      r.created_at, c.class_name, u.name AS uploader_name
               FROM classroom_resources r
               JOIN classrooms c ON c.id = r.classroom_id
               JOIN users u ON u.id = r.uploader_id
               WHERE c.faculty_id = ?
               ORDER BY r.created_at DESC""",
            (uid,),
        ).fetchall()

        attachments = conn.execute(
            """SELECT ca.attachment_name, ca.attachment_path, ca.subject, ca.created_at, c.class_name
               FROM classroom_assignments ca
               JOIN classrooms c ON c.id = ca.classroom_id
               WHERE ca.faculty_id = ? AND ca.attachment_path != ''
               GROUP BY ca.attachment_path
               ORDER BY ca.created_at DESC""",
            (uid,),
        ).fetchall()
    else:
        study_materials = conn.execute(
            """SELECT r.id, r.classroom_id, r.title, r.description, r.resource_type,
                      r.file_path, r.file_name, r.file_size, r.file_mime, r.external_url,
                      r.created_at, c.class_name, u.name AS uploader_name
               FROM classroom_resources r
               JOIN classrooms c ON c.id = r.classroom_id
               JOIN classroom_members cm ON cm.classroom_id = c.id
               JOIN users u ON u.id = r.uploader_id
               WHERE cm.student_id = ?
               ORDER BY r.created_at DESC""",
            (uid,),
        ).fetchall()

        attachments = conn.execute(
            """SELECT ca.attachment_name, ca.attachment_path, ca.subject, ca.created_at, c.class_name
               FROM classroom_assignments ca
               JOIN classrooms c ON c.id = ca.classroom_id
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE cm.student_id = ? AND ca.attachment_path != ''
               GROUP BY ca.attachment_path
               ORDER BY ca.created_at DESC""",
            (uid,),
        ).fetchall()

    conn.close()
    return render_template("resources.html", attachments=attachments, study_materials=study_materials)


def check_revision_note_access(conn, note_id: int, uid: int):
    """
    Check if a user can access a revision note.
    Access granted if:
    1. User is the uploader of the note
    2. Note is linked to a classroom and user is enrolled student or classroom faculty
    """
    note = conn.execute(
        """SELECT rn.*, u.name AS uploader_name, u.user_type AS uploader_user_type,
                  c.class_name, c.faculty_id AS class_faculty_id
           FROM revision_notes rn
           JOIN users u ON u.id = rn.uploader_id
           LEFT JOIN classrooms c ON c.id = rn.classroom_id
           WHERE rn.id = ?""",
        (note_id,),
    ).fetchone()
    if not note:
        return False, None
    if note["uploader_id"] == uid:
        return True, note
    if note["classroom_id"]:
        c_id = note["classroom_id"]
        if note["class_faculty_id"] == uid:
            return True, note
        is_member = conn.execute(
            "SELECT 1 FROM classroom_members WHERE classroom_id = ? AND student_id = ?",
            (c_id, uid),
        ).fetchone()
        if is_member:
            return True, note
    return False, None


@app.route("/revision-notes")
@login_required
def revision_notes():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    selected_subject = request.args.get("subject", "all").strip()
    selected_type = request.args.get("type", "all").strip()
    selected_content_type = request.args.get("content_type", "all").strip().lower()
    selected_classroom = request.args.get("classroom", "all").strip()
    search_query = request.args.get("q", "").strip()
    sort_by = request.args.get("sort", "newest").strip().lower()

    conn = get_db()

    # Classrooms user is in
    if user_type == "faculty":
        user_classrooms = conn.execute(
            "SELECT id, class_name, class_code FROM classrooms WHERE faculty_id=? ORDER BY class_name ASC",
            (uid,),
        ).fetchall()
    else:
        user_classrooms = conn.execute(
            """SELECT c.id, c.class_name, c.class_code
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE cm.student_id = ?
               ORDER BY c.class_name ASC""",
            (uid,),
        ).fetchall()

    # Notes visible to user:
    # 1. Notes uploaded by the user themselves
    # 2. Notes shared in any classroom the user is enrolled in or teaches
    all_notes = conn.execute(
        """SELECT rn.*, u.name AS uploader_name, u.user_type AS uploader_user_type,
                  c.class_name, c.faculty_id AS class_faculty_id
           FROM revision_notes rn
           JOIN users u ON u.id = rn.uploader_id
           LEFT JOIN classrooms c ON c.id = rn.classroom_id
           WHERE rn.uploader_id = ?
              OR (rn.classroom_id IS NOT NULL AND rn.classroom_id IN (
                    SELECT classroom_id FROM classroom_members WHERE student_id = ?
                    UNION
                    SELECT id FROM classrooms WHERE faculty_id = ?
                 ))
           ORDER BY rn.created_at DESC""",
        (uid, uid, uid),
    ).fetchall()

    # Subjects list (From accessible notes + user's subject table)
    db_subjects = [r[0] for r in conn.execute("SELECT DISTINCT subject_name FROM subjects WHERE user_id=? AND subject_name != ''", (uid,)).fetchall()]
    note_subjects = [n["subject_name"] for n in all_notes if n["subject_name"]]

    all_subject_names = sorted(
        list({s.strip() for s in (db_subjects + note_subjects) if s and s.strip()}),
        key=lambda s: s.lower(),
    )

    subject_counts = Counter()
    manual_notes_count = 0
    file_notes_count = 0
    shared_notes_count = 0
    private_notes_count = 0
    total_downloads = 0

    for n in all_notes:
        subj = n["subject_name"]
        subject_counts[subj] += 1
        if n["content_type"] == "manual":
            manual_notes_count += 1
        else:
            file_notes_count += 1
        if n["classroom_id"]:
            shared_notes_count += 1
        else:
            private_notes_count += 1
        total_downloads += (n["downloads_count"] or 0)

    # Filter notes
    filtered_notes = []
    for n in all_notes:
        if selected_subject != "all" and n["subject_name"].lower() != selected_subject.lower():
            continue
        if selected_type != "all" and n["note_type"].lower() != selected_type.lower():
            continue
        if selected_content_type in ["manual", "file"] and n["content_type"].lower() != selected_content_type:
            continue
        if selected_classroom != "all":
            if selected_classroom == "private" and n["classroom_id"]:
                continue
            elif selected_classroom == "shared" and not n["classroom_id"]:
                continue
            elif selected_classroom.isdigit() and n["classroom_id"] != int(selected_classroom):
                continue
        if search_query:
            q_lower = search_query.lower()
            text_to_search = f"{n['title']} {n['description']} {n['subject_name']} {n['uploader_name']} {n['class_name'] or ''} {n['note_type']} {n['note_content']}".lower()
            if q_lower not in text_to_search:
                continue
        filtered_notes.append(n)

    if sort_by == "downloads":
        filtered_notes.sort(key=lambda x: (x["downloads_count"] or 0), reverse=True)
    elif sort_by == "title":
        filtered_notes.sort(key=lambda x: (x["title"] or "").lower())
    elif sort_by == "oldest":
        filtered_notes.sort(key=lambda x: (x["created_at"] or ""))
    else:
        filtered_notes.sort(key=lambda x: (x["created_at"] or ""), reverse=True)

    stats = {
        "total_notes": len(all_notes),
        "total_subjects": len(all_subject_names),
        "active_subjects_count": len([s for s in all_subject_names if subject_counts[s] > 0]),
        "manual_notes_count": manual_notes_count,
        "file_notes_count": file_notes_count,
        "shared_notes_count": shared_notes_count,
        "private_notes_count": private_notes_count,
        "total_downloads": total_downloads,
    }

    conn.close()
    return render_template(
        "revision_notes.html",
        revision_notes=filtered_notes,
        all_notes=all_notes,
        subjects=all_subject_names,
        subject_counts=subject_counts,
        stats=stats,
        user_classrooms=user_classrooms,
        selected_subject=selected_subject,
        selected_type=selected_type,
        selected_content_type=selected_content_type,
        selected_classroom=selected_classroom,
        search_query=search_query,
        sort_by=sort_by,
    )


@app.route("/revision-notes/upload", methods=["POST"])
@login_required
def upload_revision_note():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")

    title = request.form.get("title", "").strip()
    subject_name = request.form.get("subject_name", "").strip()
    custom_subject = request.form.get("custom_subject", "").strip()
    note_type = request.form.get("note_type", "Lecture Notes").strip()
    content_type = request.form.get("content_type", "").strip().lower()
    note_content = request.form.get("note_content", "").strip()
    description = request.form.get("description", "").strip()
    classroom_id = request.form.get("classroom_id", "").strip()

    if not subject_name or subject_name == "__custom__":
        subject_name = custom_subject

    if not title:
        flash("Please provide a title for the revision note.", "warning")
        return redirect(url_for("revision_notes"))

    if not subject_name:
        flash("Please enter or select a subject for the revision note.", "warning")
        return redirect(url_for("revision_notes"))

    file = request.files.get("file")
    has_file = file is not None and bool(file.filename and file.filename.strip())

    if not content_type or content_type not in ["manual", "file"]:
        if has_file:
            content_type = "file"
        elif note_content:
            content_type = "manual"
        else:
            content_type = "manual"

    if content_type == "manual" and not note_content and not has_file:
        flash("Please write your manual note content or choose a file to upload.", "warning")
        return redirect(url_for("revision_notes"))

    file_path = ""
    raw_filename = ""
    file_size = 0
    file_mime = ""

    if has_file:
        raw_filename = secure_filename(file.filename)
        if not raw_filename:
            raw_filename = f"note_{int(time.time())}.pdf"

        ext = os.path.splitext(raw_filename)[1].lower()
        allowed_exts = [
            ".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md",
            ".ppt", ".pptx", ".xls", ".xlsx", ".csv",
            ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".zip", ".rar", ".7z", ".tar", ".gz"
        ]
        if ext not in allowed_exts:
            flash("Unsupported file format. Please upload PDF, Word, PowerPoint, Text, Image, or Archive files.", "danger")
            return redirect(url_for("revision_notes"))

        save_dir = os.path.join(UPLOAD_FOLDER, "revision_notes")
        os.makedirs(save_dir, exist_ok=True)

        unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{raw_filename}"
        dest_path = os.path.join(save_dir, unique_name)
        file.save(dest_path)

        file_path = os.path.join("revision_notes", unique_name).replace("\\", "/")
        try:
            file_size = os.path.getsize(dest_path)
        except Exception:
            file_size = 0
        file_mime = file.mimetype or "application/octet-stream"

    c_id = None
    if classroom_id:
        try:
            c_id = int(classroom_id)
        except ValueError:
            c_id = None

    conn = get_db()
    conn.execute(
        """INSERT INTO revision_notes
           (uploader_id, uploader_role, subject_name, title, description, content_type,
            note_content, note_type, file_path, file_name, file_size, file_mime, classroom_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, user_type, subject_name, title, description, content_type,
         note_content, note_type, file_path, raw_filename, file_size, file_mime, c_id),
    )
    conn.commit()
    conn.close()

    if c_id:
        flash(f'Revision Note "{title}" saved and shared with your classroom! 🏫📚', "success")
    else:
        flash(f'Revision Note "{title}" saved to your private notes! 🔒📚', "success")
    return redirect(url_for("revision_notes", subject=subject_name))


@app.route("/revision-notes/<int:note_id>/download")
@login_required
def download_revision_note(note_id):
    uid = session["user_id"]
    conn = get_db()
    has_access, note = check_revision_note_access(conn, note_id, uid)
    if not has_access or not note:
        conn.close()
        flash("Revision note not found or access denied.", "danger")
        return redirect(url_for("revision_notes"))

    conn.execute("UPDATE revision_notes SET downloads_count = downloads_count + 1 WHERE id=?", (note_id,))
    conn.commit()
    conn.close()

    filepath = note["file_path"]
    if filepath and not (".." in filepath or filepath.startswith("/") or filepath.startswith("\\")):
        directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
        filename = os.path.basename(filepath)

        try:
            if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) == os.path.abspath(UPLOAD_FOLDER):
                full_path = os.path.join(directory, filename)
                if os.path.exists(full_path):
                    download_name = note["file_name"] or filename
                    return send_from_directory(
                        directory,
                        filename,
                        as_attachment=True,
                        download_name=download_name,
                    )
        except Exception:
            pass

    # If it's a manual note or file doesn't exist, create a clean text file download
    note_body = note["note_content"] or note["description"] or "Revision note content."
    file_content = f"{note['title']}\nSubject: {note['subject_name']}\nType: {note['note_type']}\nAuthor: {note['uploader_name']}\nDate: {note['created_at']}\n\n{'='*50}\n\n{note_body}"
    clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', note['title'])[:40]
    filename = f"{clean_name}_Revision_Notes.txt"
    response = make_response(file_content.encode('utf-8'))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@app.route("/revision-notes/<int:note_id>/preview")
@login_required
def preview_revision_note(note_id):
    uid = session["user_id"]
    conn = get_db()
    has_access, note = check_revision_note_access(conn, note_id, uid)
    conn.close()
    if not has_access or not note:
        flash("Revision note not found or access denied.", "danger")
        return redirect(url_for("revision_notes"))

    filepath = note["file_path"]
    if filepath and not (".." in filepath or filepath.startswith("/") or filepath.startswith("\\")):
        directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
        filename = os.path.basename(filepath)

        try:
            if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) == os.path.abspath(UPLOAD_FOLDER):
                full_path = os.path.join(directory, filename)
                if os.path.exists(full_path):
                    return send_from_directory(
                        directory,
                        filename,
                        as_attachment=False,
                        mimetype=note["file_mime"] if note["file_mime"] else None,
                    )
        except Exception:
            pass

    note_body = note["note_content"] or note["description"] or "Revision note content."
    file_content = f"Title: {note['title']}\nSubject: {note['subject_name']}\nType: {note['note_type']}\nAuthor: {note['uploader_name']}\n\n{note_body}"
    response = make_response(file_content.encode('utf-8'))
    response.headers["Content-Type"] = "text/plain; charset=utf-8"
    return response


@app.route("/revision-notes/<int:note_id>/view")
@login_required
def view_revision_note_json(note_id):
    uid = session["user_id"]
    conn = get_db()
    has_access, note = check_revision_note_access(conn, note_id, uid)
    conn.close()
    if not has_access or not note:
        return jsonify({"status": "error", "message": "Note not found or access denied."}), 404

    return jsonify({
        "status": "ok",
        "note": {
            "id": note["id"],
            "title": note["title"],
            "subject_name": note["subject_name"],
            "note_type": note["note_type"],
            "content_type": note["content_type"],
            "note_content": note["note_content"],
            "description": note["description"],
            "uploader_name": note["uploader_name"],
            "uploader_role": note["uploader_role"],
            "class_name": note["class_name"],
            "classroom_id": note["classroom_id"],
            "file_name": note["file_name"],
            "file_size": note["file_size"],
            "created_at": note["created_at"],
            "downloads_count": note["downloads_count"],
        }
    })


@app.route("/revision-notes/<int:note_id>/delete", methods=["POST"])
@login_required
def delete_revision_note(note_id):
    uid = session["user_id"]
    conn = get_db()
    note = conn.execute(
        """SELECT rn.*, c.faculty_id AS class_faculty_id
           FROM revision_notes rn
           LEFT JOIN classrooms c ON c.id = rn.classroom_id
           WHERE rn.id = ?""",
        (note_id,),
    ).fetchone()
    if not note:
        conn.close()
        flash("Revision note not found.", "danger")
        return redirect(url_for("revision_notes"))

    # Uploader or the classroom's faculty can delete the note
    can_delete = (note["uploader_id"] == uid) or (note["classroom_id"] and note["class_faculty_id"] == uid)
    if not can_delete:
        conn.close()
        flash("You do not have permission to delete this note.", "danger")
        return redirect(url_for("revision_notes"))

    filepath = note["file_path"]
    if filepath and not (".." in filepath or filepath.startswith("/") or filepath.startswith("\\")):
        full_path = os.path.join(UPLOAD_FOLDER, filepath)
        try:
            if os.path.exists(full_path):
                os.remove(full_path)
        except Exception:
            pass

    conn.execute("DELETE FROM revision_notes WHERE id=?", (note_id,))
    conn.commit()
    conn.close()

    flash("Revision note removed successfully.", "info")
    return redirect(url_for("revision_notes"))


@app.route("/classrooms", methods=["GET", "POST"])
@login_required
def classrooms():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()

    if request.method == "POST":
        if user_type == "faculty":
            class_name = request.form.get("class_name", "").strip()
            if not class_name:
                flash("Please enter a classroom name.", "warning")
                return redirect(url_for("classrooms"))

            # Generate a unique class code using the DB to ensure no collisions.
            class_code = generate_unique_class_code(class_name, conn)

            conn.execute(
                "INSERT INTO classrooms (faculty_id, class_name, class_code) VALUES (?, ?, ?)",
                (uid, class_name, class_code),
            )
            conn.commit()
            flash(f'Classroom "{class_name}" created. Share code: {class_code}', "success")
            conn.close()
            return redirect(url_for("classrooms"))

        class_code = request.form.get("class_code", "").strip().upper()
        if not class_code:
            flash("Please enter a classroom code.", "warning")
            conn.close()
            return redirect(url_for("classrooms"))

        classroom = conn.execute(
            "SELECT * FROM classrooms WHERE class_code = ?",
            (class_code,),
        ).fetchone()

        if not classroom:
            flash("No classroom matches that code.", "danger")
            conn.close()
            return redirect(url_for("classrooms"))

        already_member = conn.execute(
            "SELECT 1 FROM classroom_members WHERE classroom_id=? AND student_id=?",
            (classroom["id"], uid),
        ).fetchone()
        if already_member:
            flash(f"You are already enrolled in {classroom['class_name']}.", "info")
            conn.close()
            return redirect(url_for("classrooms"))

        conn.execute(
            "INSERT INTO classroom_members (classroom_id, student_id) VALUES (?, ?)",
            (classroom["id"], uid),
        )
        conn.commit()
        flash(f"You joined {classroom['class_name']} successfully.", "success")
        conn.close()
        return redirect(url_for("classrooms"))

    selected_classroom = None
    enrolled_students = []
    selected_student_id = None
    classroom_assignments = []
    classroom_messages = []
    classroom_resources = []
    faculty_contact = None

    if user_type == "faculty":
        created_classrooms = conn.execute(
            """SELECT c.id, c.class_name, c.class_code,
                      COUNT(DISTINCT cm.student_id) AS student_count,
                      COALESCE(SUM(CASE
                          WHEN m.sender_id != c.faculty_id AND COALESCE(m.read_by_faculty, 0)=0 THEN 1
                          ELSE 0
                      END), 0) AS unread_count
               FROM classrooms c
               LEFT JOIN classroom_members cm ON cm.classroom_id = c.id
               LEFT JOIN classroom_messages m ON m.classroom_id = c.id AND m.faculty_id = c.faculty_id
               WHERE c.faculty_id=?
               GROUP BY c.id
               ORDER BY c.created_at DESC""",
            (uid,),
        ).fetchall()
        joined_classrooms = []

        requested_id = request.args.get("class_id", "").strip()
        selected_id = None
        if requested_id.isdigit():
            selected_id = int(requested_id)
        elif created_classrooms:
            selected_id = created_classrooms[0]["id"]

        if selected_id:
            selected_classroom = conn.execute(
                "SELECT id, class_name, class_code FROM classrooms WHERE id=? AND faculty_id=?",
                (selected_id, uid),
            ).fetchone()

        if selected_classroom:
            enrolled_students = conn.execute(
                """SELECT u.id, u.name, u.email, cm.joined_at,
                          (SELECT COUNT(*)
                           FROM classroom_messages m
                           WHERE m.classroom_id = cm.classroom_id
                             AND m.faculty_id = ?
                             AND m.student_id = u.id
                             AND m.sender_id = u.id
                             AND COALESCE(m.read_by_faculty, 0)=0) AS unread_count
                    FROM classroom_members cm
                    JOIN users u ON u.id = cm.student_id
                    WHERE cm.classroom_id=?
                    ORDER BY u.name COLLATE NOCASE""",
                (uid, selected_classroom["id"]),
            ).fetchall()

            requested_student = request.args.get("student_id", "").strip()
            if requested_student.isdigit() and any(s["id"] == int(requested_student) for s in enrolled_students):
                selected_student_id = int(requested_student)
            elif enrolled_students:
                selected_student_id = enrolled_students[0]["id"]

            classroom_assignments = conn.execute(
                """SELECT ca.id, ca.task_name, ca.subject, ca.deadline, ca.instructions, ca.status,
                          ca.borrowed, ca.started, ca.created_at, u.name AS student_name, u.email AS student_email,
                          ca.student_id,
                          COALESCE(ca.attachment_path,'') AS attachment_path,
                          COALESCE(ca.attachment_name,'') AS attachment_name,
                          COALESCE(ca.attachment_mime,'') AS attachment_mime,
                          sub.id AS submission_id,
                          COALESCE(sub.submission_text, '') AS submission_text,
                          COALESCE(sub.file_path, '') AS submission_file_path,
                          COALESCE(sub.file_name, '') AS submission_file_name,
                          COALESCE(sub.file_size, 0) AS submission_file_size,
                          COALESCE(sub.file_mime, '') AS submission_file_mime,
                          sub.submitted_at AS submission_time,
                          COALESCE(sub.status, '') AS submission_status,
                          COALESCE(sub.grade, '') AS submission_grade,
                          COALESCE(sub.feedback, '') AS submission_feedback
                   FROM classroom_assignments ca
                   JOIN users u ON u.id = ca.student_id
                   LEFT JOIN classroom_submissions sub ON sub.assignment_id = ca.id AND sub.student_id = ca.student_id
                   WHERE ca.classroom_id=? AND ca.faculty_id=?
                   ORDER BY ca.created_at DESC""",
                (selected_classroom["id"], uid),
            ).fetchall()

            classroom_resources = conn.execute(
                """SELECT r.id, r.classroom_id, r.uploader_id, r.title, r.description,
                          r.resource_type, r.file_path, r.file_name, r.file_size, r.file_mime,
                          r.external_url, r.created_at, u.name AS uploader_name
                   FROM classroom_resources r
                   JOIN users u ON u.id = r.uploader_id
                   WHERE r.classroom_id=?
                   ORDER BY r.created_at DESC""",
                (selected_classroom["id"],),
            ).fetchall()

            if selected_student_id:
                conn.execute(
                    """UPDATE classroom_messages
                       SET read_by_faculty=1
                       WHERE classroom_id=? AND faculty_id=? AND student_id=? AND sender_id!=?""",
                    (selected_classroom["id"], uid, selected_student_id, uid),
                )
                conn.commit()
                classroom_messages = conn.execute(
                    """SELECT m.id, m.sender_id, m.message, m.created_at, u.name AS sender_name,
                                 COALESCE(m.attachment_path,'') AS attachment_path,
                                 COALESCE(m.attachment_name,'') AS attachment_name,
                                 COALESCE(m.attachment_mime,'') AS attachment_mime
                       FROM classroom_messages m
                       JOIN users u ON u.id = m.sender_id
                       WHERE m.classroom_id=? AND m.faculty_id=? AND m.student_id=?
                       ORDER BY m.created_at ASC""",
                    (selected_classroom["id"], uid, selected_student_id),
                ).fetchall()
    else:
        created_classrooms = []
        joined_classrooms = conn.execute(
            """SELECT c.id, c.class_name, c.class_code, c.faculty_id, u.name AS faculty_name,
                      (SELECT COUNT(*)
                       FROM classroom_messages m
                       WHERE m.classroom_id = c.id
                         AND m.student_id = ?
                         AND m.sender_id = c.faculty_id
                         AND COALESCE(m.read_by_student, 0)=0) AS unread_count
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               JOIN users u ON u.id = c.faculty_id
               WHERE cm.student_id=?
               ORDER BY c.created_at DESC""",
            (uid, uid),
        ).fetchall()

        requested_id = request.args.get("class_id", "").strip()
        selected_id = None
        if requested_id.isdigit():
            selected_id = int(requested_id)
        elif joined_classrooms:
            selected_id = joined_classrooms[0]["id"]

        if selected_id:
            selected_classroom = conn.execute(
                """SELECT c.id, c.class_name, c.class_code, c.faculty_id, u.name AS faculty_name
                   FROM classrooms c
                   JOIN users u ON u.id = c.faculty_id
                   JOIN classroom_members cm ON cm.classroom_id = c.id AND cm.student_id=?
                   WHERE c.id=?""",
                (uid, selected_id),
            ).fetchone()

        if selected_classroom:
            faculty_contact = {
                "id": selected_classroom["faculty_id"],
                "name": selected_classroom["faculty_name"],
            }

            conn.execute(
                """UPDATE classroom_messages
                   SET read_by_student=1
                   WHERE classroom_id=? AND faculty_id=? AND student_id=? AND sender_id!=?""",
                (selected_classroom["id"], selected_classroom["faculty_id"], uid, uid),
            )
            conn.commit()

            classroom_assignments = conn.execute(
                """SELECT ca.id, ca.task_name, ca.subject, ca.deadline, ca.instructions, ca.status,
                          ca.borrowed, ca.started, ca.created_at,
                          COALESCE(ca.attachment_path,'') AS attachment_path,
                          COALESCE(ca.attachment_name,'') AS attachment_name,
                          COALESCE(ca.attachment_mime,'') AS attachment_mime,
                          sub.id AS submission_id,
                          COALESCE(sub.submission_text, '') AS submission_text,
                          COALESCE(sub.file_path, '') AS submission_file_path,
                          COALESCE(sub.file_name, '') AS submission_file_name,
                          COALESCE(sub.file_size, 0) AS submission_file_size,
                          COALESCE(sub.file_mime, '') AS submission_file_mime,
                          sub.submitted_at AS submission_time,
                          COALESCE(sub.status, '') AS submission_status,
                          COALESCE(sub.grade, '') AS submission_grade,
                          COALESCE(sub.feedback, '') AS submission_feedback
                   FROM classroom_assignments ca
                   LEFT JOIN classroom_submissions sub ON sub.assignment_id = ca.id AND sub.student_id = ?
                   WHERE ca.classroom_id=? AND ca.student_id=?
                   ORDER BY ca.created_at DESC""",
                (uid, selected_classroom["id"], uid),
            ).fetchall()

            classroom_resources = conn.execute(
                """SELECT r.id, r.classroom_id, r.uploader_id, r.title, r.description,
                          r.resource_type, r.file_path, r.file_name, r.file_size, r.file_mime,
                          r.external_url, r.created_at, u.name AS uploader_name
                   FROM classroom_resources r
                   JOIN users u ON u.id = r.uploader_id
                   WHERE r.classroom_id=?
                   ORDER BY r.created_at DESC""",
                (selected_classroom["id"],),
            ).fetchall()

            classroom_messages = conn.execute(
                """SELECT m.id, m.sender_id, m.message, m.created_at, u.name AS sender_name,
                             COALESCE(m.attachment_path,'') AS attachment_path,
                             COALESCE(m.attachment_name,'') AS attachment_name,
                             COALESCE(m.attachment_mime,'') AS attachment_mime
                   FROM classroom_messages m
                   JOIN users u ON u.id = m.sender_id
                   WHERE m.classroom_id=? AND m.faculty_id=? AND m.student_id=?
                   ORDER BY m.created_at ASC""",
                (selected_classroom["id"], selected_classroom["faculty_id"], uid),
            ).fetchall()

    conn.close()
    return render_template(
        "classrooms.html",
        user_type=user_type,
        created_classrooms=created_classrooms,
        joined_classrooms=joined_classrooms,
        selected_classroom=selected_classroom,
        enrolled_students=enrolled_students,
        selected_student_id=selected_student_id,
        classroom_assignments=classroom_assignments,
        classroom_messages=classroom_messages,
        classroom_resources=classroom_resources,
        faculty_contact=faculty_contact,
    )


@app.route("/classrooms/enroll", methods=["POST"])
@login_required
def enroll_student_by_faculty():
    if session.get("user_type") != "faculty":
        flash("Only faculty can enroll students.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    classroom_id_raw = request.form.get("classroom_id", "").strip()
    student_email = request.form.get("student_email", "").strip().lower()

    if not classroom_id_raw.isdigit() or not student_email:
        flash("Please choose a classroom and enter a student email.", "warning")
        return redirect(url_for("classrooms"))

    classroom_id = int(classroom_id_raw)
    conn = get_db()

    classroom = conn.execute(
        "SELECT id, class_name FROM classrooms WHERE id=? AND faculty_id=?",
        (classroom_id, uid),
    ).fetchone()
    if not classroom:
        conn.close()
        flash("Classroom not found.", "danger")
        return redirect(url_for("classrooms"))

    student = conn.execute(
        "SELECT id, name, user_type FROM users WHERE lower(email)=?",
        (student_email,),
    ).fetchone()
    if not student:
        conn.close()
        flash("No user found with that email.", "danger")
        return redirect(url_for("classrooms", class_id=classroom_id))

    if student["user_type"] != "student":
        conn.close()
        flash("Only student accounts can be enrolled.", "warning")
        return redirect(url_for("classrooms", class_id=classroom_id))

    existing = conn.execute(
        "SELECT 1 FROM classroom_members WHERE classroom_id=? AND student_id=?",
        (classroom_id, student["id"]),
    ).fetchone()
    if existing:
        conn.close()
        flash(f"{student['name']} is already enrolled.", "info")
        return redirect(url_for("classrooms", class_id=classroom_id))

    conn.execute(
        "INSERT INTO classroom_members (classroom_id, student_id) VALUES (?, ?)",
        (classroom_id, student["id"]),
    )
    conn.commit()
    conn.close()
    flash(f"{student['name']} enrolled successfully.", "success")
    return redirect(url_for("classrooms", class_id=classroom_id, student_id=student["id"]))


@app.route("/classrooms/assign-task", methods=["POST"])
@login_required
def classroom_assign_task():
    if session.get("user_type") != "faculty":
        flash("Only faculty can assign classroom tasks.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    classroom_id_raw = request.form.get("classroom_id", "").strip()
    target_student_raw = request.form.get("target_student_id", "all").strip()
    task_name = request.form.get("task_name", "").strip()
    subject = request.form.get("subject", "").strip() or "General"
    deadline = request.form.get("deadline", "").strip()
    instructions = request.form.get("instructions", "").strip()
    attachment = request.files.get("attachment")
    attachment_path = ''
    attachment_name = ''
    attachment_mime = ''
    if attachment and attachment.filename:
        save_dir = os.path.join(UPLOAD_FOLDER, 'classroom_attachments')
        os.makedirs(save_dir, exist_ok=True)
        fname = secure_filename(attachment.filename)
        unique = f"{int(datetime.utcnow().timestamp())}_{secrets.token_hex(6)}_{fname}"
        dest = os.path.join(save_dir, unique)
        attachment.save(dest)
        attachment_path = os.path.join('classroom_attachments', unique)
        attachment_name = attachment.filename
        attachment_mime = attachment.mimetype or ''

    if not classroom_id_raw.isdigit() or not all([task_name, deadline]):
        flash("Please complete task name, deadline, and classroom.", "warning")
        return redirect(url_for("classrooms"))

    try:
        deadline_dt = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        flash("Please choose a valid deadline date.", "danger")
        return redirect(url_for("classrooms"))

    if deadline_dt < date.today():
        flash("Deadline cannot be in the past.", "warning")
        return redirect(url_for("classrooms"))

    classroom_id = int(classroom_id_raw)
    conn = get_db()
    classroom = conn.execute(
        "SELECT id, class_name FROM classrooms WHERE id=? AND faculty_id=?",
        (classroom_id, uid),
    ).fetchone()
    if not classroom:
        conn.close()
        flash("Classroom not found.", "danger")
        return redirect(url_for("classrooms"))

    member_rows = conn.execute(
        "SELECT student_id FROM classroom_members WHERE classroom_id=?",
        (classroom_id,),
    ).fetchall()
    member_ids = [row["student_id"] for row in member_rows]

    if not member_ids:
        conn.close()
        flash("No enrolled students found in this classroom.", "warning")
        return redirect(url_for("classrooms", class_id=classroom_id))

    if target_student_raw == "all":
        targets = member_ids
    elif target_student_raw.isdigit() and int(target_student_raw) in member_ids:
        targets = [int(target_student_raw)]
    else:
        conn.close()
        flash("Invalid student selected for assignment.", "danger")
        return redirect(url_for("classrooms", class_id=classroom_id))

    conn.executemany(
        """INSERT INTO classroom_assignments
           (classroom_id, faculty_id, student_id, task_name, subject, deadline, instructions, status,
            attachment_path, attachment_name, attachment_mime)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'Assigned', ?, ?, ?)""",
        [
            (
                classroom_id,
                uid,
                sid,
                task_name,
                subject,
                deadline,
                instructions,
                attachment_path,
                attachment_name,
                attachment_mime,
            )
            for sid in targets
        ],
    )
    conn.commit()
    conn.close()

    if len(targets) == 1:
        flash("Task assigned to selected student.", "success")
        return redirect(url_for("classrooms", class_id=classroom_id, student_id=targets[0]))

    flash(f"Task assigned to {len(targets)} students.", "success")
    return redirect(url_for("classrooms", class_id=classroom_id))


@app.route("/classrooms/chat/send", methods=["POST"])
@login_required
def classroom_send_message():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    classroom_id_raw = request.form.get("classroom_id", "").strip()
    message = request.form.get("message", "").strip()
    recipient_raw = request.form.get("recipient_student_id", "").strip()

    if not classroom_id_raw.isdigit() or not message:
        flash("Please choose a classroom and enter a message.", "warning")
        return redirect(url_for("classrooms"))

    classroom_id = int(classroom_id_raw)
    conn = get_db()

    if user_type == "faculty":
        classroom = conn.execute(
            "SELECT id, faculty_id FROM classrooms WHERE id=? AND faculty_id=?",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            flash("Classroom not found.", "danger")
            return redirect(url_for("classrooms"))
        # Allow faculty to send to a single student by id or broadcast to all students using 'all'
        # Handle optional attachment for faculty messages
        attachment = request.files.get("attachment")
        attachment_path = ""
        attachment_name = ""
        attachment_mime = ""
        if attachment and attachment.filename:
            save_dir = os.path.join(UPLOAD_FOLDER, "classroom_messages")
            os.makedirs(save_dir, exist_ok=True)
            fname = secure_filename(attachment.filename)
            unique = f"{int(datetime.utcnow().timestamp())}_{secrets.token_hex(6)}_{fname}"
            dest = os.path.join(save_dir, unique)
            attachment.save(dest)
            attachment_path = os.path.join("classroom_messages", unique)
            attachment_name = attachment.filename
            attachment_mime = attachment.mimetype or ""

        # gather target student ids
        if recipient_raw == "all":
            member_rows = conn.execute(
                "SELECT student_id FROM classroom_members WHERE classroom_id=?",
                (classroom_id,),
            ).fetchall()
            targets = [r["student_id"] for r in member_rows]
            if not targets:
                conn.close()
                flash("No enrolled students to send message.", "warning")
                return redirect(url_for("classrooms", class_id=classroom_id))
        elif recipient_raw.isdigit():
            student_id = int(recipient_raw)
            member = conn.execute(
                "SELECT 1 FROM classroom_members WHERE classroom_id=? AND student_id=?",
                (classroom_id, student_id),
            ).fetchone()
            if not member:
                conn.close()
                flash("Student is not part of this classroom.", "danger")
                return redirect(url_for("classrooms", class_id=classroom_id))
            targets = [student_id]
        else:
            conn.close()
            flash("Select a student or choose 'All' to broadcast.", "warning")
            return redirect(url_for("classrooms", class_id=classroom_id))

        faculty_id = uid

        # insert a message row for each target student
        for sid in targets:
            conn.execute(
                """INSERT INTO classroom_messages
                   (classroom_id, faculty_id, student_id, sender_id, message, read_by_faculty, read_by_student,
                    attachment_path, attachment_name, attachment_mime)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    classroom_id,
                    faculty_id,
                    sid,
                    uid,
                    message,
                    1,
                    0,
                    attachment_path,
                    attachment_name,
                    attachment_mime,
                ),
            )
    else:
        classroom = conn.execute(
            """SELECT c.id, c.faculty_id
               FROM classrooms c
               JOIN classroom_members cm ON cm.classroom_id = c.id
               WHERE c.id=? AND cm.student_id=?""",
            (classroom_id, uid),
        ).fetchone()
        if not classroom:
            conn.close()
            flash("Classroom not found.", "danger")
            return redirect(url_for("classrooms"))

        faculty_id = classroom["faculty_id"]
        student_id = uid

        # Handle optional attachment for messages
        attachment = request.files.get("attachment")
        attachment_path = ''
        attachment_name = ''
        attachment_mime = ''
        if attachment and attachment.filename:
            save_dir = os.path.join(UPLOAD_FOLDER, 'classroom_messages')
            os.makedirs(save_dir, exist_ok=True)
            fname = secure_filename(attachment.filename)
            unique = f"{int(datetime.utcnow().timestamp())}_{secrets.token_hex(6)}_{fname}"
            dest = os.path.join(save_dir, unique)
            attachment.save(dest)
            attachment_path = os.path.join('classroom_messages', unique)
            attachment_name = attachment.filename
            attachment_mime = attachment.mimetype or ''

        conn.execute(
            """INSERT INTO classroom_messages
               (classroom_id, faculty_id, student_id, sender_id, message, read_by_faculty, read_by_student,
                attachment_path, attachment_name, attachment_mime)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                classroom_id,
                faculty_id,
                student_id,
                uid,
                message,
                1 if user_type == "faculty" else 0,
                1 if user_type == "student" else 0,
                attachment_path,
                attachment_name,
                attachment_mime,
            ),
        )
    conn.commit()
    conn.close()

    # If faculty sent the message, only include `student_id` in the redirect
    # when a single student was targeted. Broadcasting to all students should
    # redirect to the classroom view without a specific student selected.
    if user_type == "faculty":
        try:
            # If a single student was targeted, `recipient_raw` will be a digit
            # and `student_id` will have been set earlier. Otherwise omit it.
            if recipient_raw.isdigit():
                return redirect(url_for("classrooms", class_id=classroom_id, student_id=int(recipient_raw)))
        except Exception:
            pass
        return redirect(url_for("classrooms", class_id=classroom_id))

    return redirect(url_for("classrooms", class_id=classroom_id))


@app.route("/classrooms/assignments/<int:assignment_id>/borrow", methods=["POST"])
@login_required
def borrow_classroom_assignment(assignment_id):
    if session.get("user_type") != "student":
        flash("Only students can borrow assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    assignment = conn.execute(
         """SELECT ca.id, ca.classroom_id, ca.student_id, ca.task_name, ca.subject, ca.deadline,
                ca.borrowed, ca.borrowed_task_id,
                COALESCE(ca.attachment_path,'') AS attachment_path,
                COALESCE(ca.attachment_name,'') AS attachment_name,
                COALESCE(ca.attachment_mime,'') AS attachment_mime
            FROM classroom_assignments ca
           JOIN classroom_members cm ON cm.classroom_id = ca.classroom_id AND cm.student_id = ?
           WHERE ca.id=? AND ca.student_id=?""",
        (uid, assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found.", "danger")
        return redirect(url_for("classrooms"))

    if int(assignment["borrowed"] or 0) == 1 and assignment["borrowed_task_id"]:
        conn.close()
        flash("Assignment already borrowed into your tasks.", "info")
        return redirect(url_for("classrooms", class_id=assignment["classroom_id"]))

    cursor = conn.execute(
        "INSERT INTO tasks (user_id, task_name, subject, deadline, status, attachment_path, attachment_name, attachment_mime) VALUES (?,?,?,?,?,?,?,?)",
        (
            uid,
            assignment["task_name"],
            assignment["subject"],
            assignment["deadline"],
            'Pending',
            (assignment["attachment_path"] or "") if "attachment_path" in assignment.keys() else "",
            (assignment["attachment_name"] or "") if "attachment_name" in assignment.keys() else "",
            (assignment["attachment_mime"] or "") if "attachment_mime" in assignment.keys() else "",
        ),
    )
    task_id = cursor.lastrowid
    conn.execute(
        "UPDATE classroom_assignments SET borrowed=1, borrowed_task_id=? WHERE id=?",
        (task_id, assignment_id),
    )
    conn.commit()
    conn.close()

    flash("Assignment borrowed to your task list.", "success")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"]))


@app.route('/classrooms/attachment/<path:filepath>')
@login_required
def classroom_attachment_download(filepath):
    # Prevent path traversal
    if '..' in filepath or filepath.startswith('/') or filepath.startswith('\\'):
        abort(400)
    directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
    filename = os.path.basename(filepath)
    # Ensure directory is inside UPLOAD_FOLDER
    try:
        if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) != os.path.abspath(UPLOAD_FOLDER):
            abort(400)
    except Exception:
        abort(400)
    if not os.path.exists(os.path.join(directory, filename)):
        abort(404)
    return send_from_directory(directory, filename, as_attachment=True)


@app.route('/classrooms/<int:class_id>/delete', methods=['POST'])
@login_required
def delete_classroom(class_id):
    if session.get('user_type') != 'faculty':
        flash('Only faculty can delete classrooms.', 'danger')
        return redirect(url_for('classrooms'))
    uid = session['user_id']
    conn = get_db()
    classroom = conn.execute('SELECT id, class_name FROM classrooms WHERE id=? AND faculty_id=?', (class_id, uid)).fetchone()
    if not classroom:
        conn.close()
        flash('Classroom not found or not owned by you.', 'danger')
        return redirect(url_for('classrooms'))

    # Server-side confirmation: require exact classroom name match
    confirm_name = (request.form.get('confirm_name') or '').strip()
    expected_name = (classroom['class_name'] or '').strip()
    if not confirm_name or confirm_name != expected_name:
        conn.close()
        flash('Classroom name confirmation did not match. Deletion cancelled.', 'warning')
        return redirect(url_for('classrooms', class_id=class_id))

    # Remove files referenced by assignments, messages, and study materials
    for row in conn.execute('SELECT attachment_path FROM classroom_assignments WHERE classroom_id=?', (class_id,)).fetchall():
        ap = row['attachment_path'] if row and row['attachment_path'] else ''
        if ap:
            fpath = os.path.join(UPLOAD_FOLDER, ap)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

    for row in conn.execute('SELECT attachment_path FROM classroom_messages WHERE classroom_id=?', (class_id,)).fetchall():
        ap = row['attachment_path'] if row and row['attachment_path'] else ''
        if ap:
            fpath = os.path.join(UPLOAD_FOLDER, ap)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

    for row in conn.execute('SELECT file_path FROM classroom_resources WHERE classroom_id=?', (class_id,)).fetchall():
        fp = row['file_path'] if row and row['file_path'] else ''
        if fp:
            fpath = os.path.join(UPLOAD_FOLDER, fp)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

    for row in conn.execute('SELECT file_path FROM classroom_submissions WHERE classroom_id=?', (class_id,)).fetchall():
        fp = row['file_path'] if row and row['file_path'] else ''
        if fp:
            fpath = os.path.join(UPLOAD_FOLDER, fp)
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

    # Delete DB rows
    conn.execute('DELETE FROM classroom_submissions WHERE classroom_id=?', (class_id,))
    conn.execute('DELETE FROM classroom_resources WHERE classroom_id=?', (class_id,))
    conn.execute('DELETE FROM classroom_messages WHERE classroom_id=?', (class_id,))
    conn.execute('DELETE FROM classroom_assignments WHERE classroom_id=?', (class_id,))
    conn.execute('DELETE FROM classroom_members WHERE classroom_id=?', (class_id,))
    conn.execute('DELETE FROM classrooms WHERE id=? AND faculty_id=?', (class_id, uid))
    conn.commit()
    conn.close()
    flash('Classroom and its data deleted.', 'info')
    return redirect(url_for('classrooms'))


@app.route("/classrooms/<int:class_id>/students/<int:student_id>/remove", methods=["POST"])
@login_required
def remove_student_from_classroom(class_id, student_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty members have permission to remove students from classrooms.", "danger")
        return redirect(url_for("settings"))

    uid = session["user_id"]
    conn = get_db()

    # Verify that the classroom exists and is owned by the current faculty
    classroom = conn.execute(
        "SELECT id, class_name FROM classrooms WHERE id=? AND faculty_id=?",
        (class_id, uid),
    ).fetchone()

    if not classroom:
        conn.close()
        flash("Classroom not found or you do not have permission to manage this classroom.", "danger")
        return redirect(url_for("settings"))

    # Verify that the student is enrolled in this classroom
    student = conn.execute(
        """SELECT u.id, u.name, u.email
           FROM users u
           JOIN classroom_members cm ON cm.student_id = u.id
           WHERE cm.classroom_id=? AND u.id=?""",
        (class_id, student_id),
    ).fetchone()

    if not student:
        conn.close()
        flash("The selected student is not enrolled in this classroom.", "warning")
        return redirect(request.referrer or url_for("settings"))

    # Remove enrollment and associated records for this student in this classroom
    conn.execute(
        "DELETE FROM classroom_members WHERE classroom_id=? AND student_id=?",
        (class_id, student_id),
    )
    conn.execute(
        "DELETE FROM classroom_assignments WHERE classroom_id=? AND student_id=?",
        (class_id, student_id),
    )
    conn.execute(
        "DELETE FROM classroom_messages WHERE classroom_id=? AND student_id=?",
        (class_id, student_id),
    )
    conn.commit()
    conn.close()

    flash(f"Student \"{student['name']}\" ({student['email']}) has been successfully removed from \"{classroom['class_name']}\".", "success")
    return redirect(request.referrer or url_for("settings"))


# ── Classroom Study Materials / Resources Routes ──────────────────────────────
@app.route("/classrooms/<int:class_id>/resources/upload", methods=["POST"])
@login_required
def upload_classroom_resource(class_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty members can upload study materials.", "danger")
        return redirect(url_for("classrooms", class_id=class_id))

    uid = session["user_id"]
    conn = get_db()
    classroom = conn.execute(
        "SELECT id, class_name FROM classrooms WHERE id=? AND faculty_id=?",
        (class_id, uid),
    ).fetchone()

    if not classroom:
        conn.close()
        flash("Classroom not found or you do not have permission to upload materials.", "danger")
        return redirect(url_for("classrooms"))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    resource_type_input = request.form.get("resource_type", "file").strip().lower()
    external_url = request.form.get("external_url", "").strip()

    if not title:
        conn.close()
        flash("Please provide a title for the study material.", "warning")
        return redirect(url_for("classrooms", class_id=class_id, tab="tab-resources"))

    file_path = ""
    file_name = ""
    file_size = 0
    file_mime = ""
    resource_type = "file"

    if resource_type_input == "link":
        if not external_url:
            conn.close()
            flash("Please provide a valid web link or URL.", "warning")
            return redirect(url_for("classrooms", class_id=class_id, tab="tab-resources"))
        if not (external_url.startswith("http://") or external_url.startswith("https://")):
            external_url = "https://" + external_url
        resource_type = "link"
    else:
        file = request.files.get("resource_file")
        if not file or not file.filename:
            conn.close()
            flash("Please select a file to upload.", "warning")
            return redirect(url_for("classrooms", class_id=class_id, tab="tab-resources"))

        raw_filename = secure_filename(file.filename)
        if not raw_filename:
            raw_filename = "study_material"

        file_name = file.filename
        file_mime = file.content_type or "application/octet-stream"

        ext = os.path.splitext(file_name)[1].lower()
        if ext in [".pdf"]:
            resource_type = "pdf"
        elif ext in [".doc", ".docx", ".odt", ".rtf", ".txt", ".md"]:
            resource_type = "doc"
        elif ext in [".ppt", ".pptx"]:
            resource_type = "presentation"
        elif ext in [".xls", ".xlsx", ".csv"]:
            resource_type = "spreadsheet"
        elif ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
            resource_type = "image"
        elif ext in [".zip", ".rar", ".7z", ".tar", ".gz"]:
            resource_type = "archive"
        else:
            resource_type = "file"

        save_dir = os.path.join(UPLOAD_FOLDER, "classroom_resources")
        os.makedirs(save_dir, exist_ok=True)

        unique_name = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{raw_filename}"
        dest_path = os.path.join(save_dir, unique_name)
        file.save(dest_path)

        file_path = os.path.join("classroom_resources", unique_name).replace("\\", "/")
        try:
            file_size = os.path.getsize(dest_path)
        except Exception:
            file_size = 0

    conn.execute(
        """INSERT INTO classroom_resources
           (classroom_id, uploader_id, title, description, resource_type, file_path, file_name, file_size, file_mime, external_url)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (class_id, uid, title, description, resource_type, file_path, file_name, file_size, file_mime, external_url),
    )
    conn.commit()
    conn.close()

    flash(f'Study material "{title}" uploaded successfully! 📚', "success")
    return redirect(url_for("classrooms", class_id=class_id, tab="tab-resources"))


@app.route("/classrooms/resources/<int:resource_id>/download")
@login_required
def download_classroom_resource(resource_id):
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()
    resource = conn.execute(
        "SELECT * FROM classroom_resources WHERE id=?",
        (resource_id,),
    ).fetchone()

    if not resource:
        conn.close()
        flash("Study material not found.", "danger")
        return redirect(url_for("classrooms"))

    classroom_id = resource["classroom_id"]
    has_access = False

    if user_type == "faculty":
        owner = conn.execute(
            "SELECT 1 FROM classrooms WHERE id=? AND faculty_id=?",
            (classroom_id, uid),
        ).fetchone()
        if owner:
            has_access = True
    else:
        member = conn.execute(
            "SELECT 1 FROM classroom_members WHERE classroom_id=? AND student_id=?",
            (classroom_id, uid),
        ).fetchone()
        if member:
            has_access = True

    conn.close()

    if not has_access:
        flash("You do not have permission to access resources from this classroom.", "danger")
        return redirect(url_for("classrooms"))

    if resource["resource_type"] == "link" and resource["external_url"]:
        return redirect(resource["external_url"])

    filepath = resource["file_path"]
    if not filepath or ".." in filepath or filepath.startswith("/") or filepath.startswith("\\"):
        abort(400)

    directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
    filename = os.path.basename(filepath)

    try:
        if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) != os.path.abspath(UPLOAD_FOLDER):
            abort(400)
    except Exception:
        abort(400)

    full_path = os.path.join(directory, filename)
    if not os.path.exists(full_path):
        flash("The requested resource file was not found on the server.", "danger")
        return redirect(url_for("classrooms", class_id=classroom_id))

    download_name = resource["file_name"] or filename
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/classrooms/resources/<int:resource_id>/preview")
@login_required
def preview_classroom_resource(resource_id):
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()
    resource = conn.execute(
        "SELECT * FROM classroom_resources WHERE id=?",
        (resource_id,),
    ).fetchone()

    if not resource:
        conn.close()
        flash("Study material not found.", "danger")
        return redirect(url_for("classrooms"))

    classroom_id = resource["classroom_id"]
    has_access = False

    if user_type == "faculty":
        owner = conn.execute(
            "SELECT 1 FROM classrooms WHERE id=? AND faculty_id=?",
            (classroom_id, uid),
        ).fetchone()
        if owner:
            has_access = True
    else:
        member = conn.execute(
            "SELECT 1 FROM classroom_members WHERE classroom_id=? AND student_id=?",
            (classroom_id, uid),
        ).fetchone()
        if member:
            has_access = True

    conn.close()

    if not has_access:
        flash("You do not have permission to view this resource.", "danger")
        return redirect(url_for("classrooms"))

    if resource["resource_type"] == "link" and resource["external_url"]:
        return redirect(resource["external_url"])

    filepath = resource["file_path"]
    if not filepath or ".." in filepath or filepath.startswith("/") or filepath.startswith("\\"):
        abort(400)

    directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
    filename = os.path.basename(filepath)

    try:
        if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) != os.path.abspath(UPLOAD_FOLDER):
            abort(400)
    except Exception:
        abort(400)

    full_path = os.path.join(directory, filename)
    if not os.path.exists(full_path):
        flash("The requested file was not found on the server.", "danger")
        return redirect(url_for("classrooms", class_id=classroom_id))

    return send_from_directory(
        directory,
        filename,
        as_attachment=False,
        mimetype=resource["file_mime"] if resource["file_mime"] else None,
    )


@app.route("/classrooms/resources/<int:resource_id>/delete", methods=["POST"])
@login_required
def delete_classroom_resource(resource_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty members can delete study materials.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    resource = conn.execute(
        """SELECT r.*, c.faculty_id
           FROM classroom_resources r
           JOIN classrooms c ON c.id = r.classroom_id
           WHERE r.id=?""",
        (resource_id,),
    ).fetchone()

    if not resource or resource["faculty_id"] != uid:
        conn.close()
        flash("Resource not found or you do not have permission to delete it.", "danger")
        return redirect(url_for("classrooms"))

    classroom_id = resource["classroom_id"]
    filepath = resource["file_path"]
    if filepath:
        fpath = os.path.join(UPLOAD_FOLDER, filepath)
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception:
            pass

    conn.execute("DELETE FROM classroom_resources WHERE id=?", (resource_id,))
    conn.commit()
    conn.close()

    flash("Study material removed.", "info")
    return redirect(url_for("classrooms", class_id=classroom_id, tab="tab-resources"))


@app.route("/classrooms/assignments/<int:assignment_id>/play", methods=["POST"])
@login_required
def play_classroom_assignment(assignment_id):
    if session.get("user_type") != "student":
        flash("Only students can start classroom assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    assignment = conn.execute(
        """SELECT ca.id, ca.classroom_id, ca.student_id, ca.borrowed_task_id
           FROM classroom_assignments ca
           JOIN classroom_members cm ON cm.classroom_id = ca.classroom_id AND cm.student_id = ?
           WHERE ca.id=? AND ca.student_id=?""",
        (uid, assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found.", "danger")
        return redirect(url_for("classrooms"))

    conn.execute(
        "UPDATE classroom_assignments SET started=1, status='In Progress' WHERE id=?",
        (assignment_id,),
    )

    if assignment["borrowed_task_id"]:
        conn.execute(
            "UPDATE tasks SET status='Pending' WHERE id=? AND user_id=?",
            (assignment["borrowed_task_id"], uid),
        )

    conn.commit()
    conn.close()
    flash("Assignment opened. Keep going!", "success")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"]))


@app.route("/classrooms/assignments/<int:assignment_id>/complete", methods=["POST"])
@login_required
def complete_classroom_assignment(assignment_id):
    if session.get("user_type") != "student":
        flash("Only students can complete classroom assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    assignment = conn.execute(
        """SELECT ca.id, ca.classroom_id, ca.student_id, ca.borrowed_task_id
           FROM classroom_assignments ca
           JOIN classroom_members cm ON cm.classroom_id = ca.classroom_id AND cm.student_id = ?
           WHERE ca.id=? AND ca.student_id=?""",
        (uid, assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found.", "danger")
        return redirect(url_for("classrooms"))

    conn.execute(
        "UPDATE classroom_assignments SET status='Completed', started=1 WHERE id=?",
        (assignment_id,),
    )

    if assignment["borrowed_task_id"]:
        conn.execute(
            "UPDATE tasks SET status='Completed' WHERE id=? AND user_id=?",
            (assignment["borrowed_task_id"], uid),
        )

    conn.commit()
    conn.close()
    flash("Classroom assignment marked as completed.", "success")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"]))


@app.route("/classrooms/assignments/<int:assignment_id>/edit", methods=["POST"])
@login_required
def edit_classroom_assignment(assignment_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty can edit assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    task_name = request.form.get("task_name", "").strip()
    subject = request.form.get("subject", "").strip() or "General"
    deadline = request.form.get("deadline", "").strip()
    instructions = request.form.get("instructions", "").strip()

    if not all([task_name, deadline]):
        flash("Task name and deadline are required.", "warning")
        return redirect(url_for("classrooms"))

    try:
        deadline_dt = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        flash("Please choose a valid deadline date.", "danger")
        return redirect(url_for("classrooms"))

    if deadline_dt < date.today():
        flash("Deadline cannot be in the past.", "warning")
        return redirect(url_for("classrooms"))

    conn = get_db()
    assignment = conn.execute(
        """SELECT id, classroom_id, student_id
           FROM classroom_assignments
           WHERE id=? AND faculty_id=?""",
        (assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found.", "danger")
        return redirect(url_for("classrooms"))

    conn.execute(
        """UPDATE classroom_assignments
           SET task_name=?, subject=?, deadline=?, instructions=?
           WHERE id=? AND faculty_id=?""",
        (task_name, subject, deadline, instructions, assignment_id, uid),
    )
    conn.commit()
    conn.close()
    flash("Classroom assignment updated.", "success")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"], student_id=assignment["student_id"]))


@app.route("/classrooms/assignments/<int:assignment_id>/submit", methods=["POST"])
@login_required
def submit_classroom_assignment(assignment_id):
    if session.get("user_type") != "student":
        flash("Only students can submit assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    assignment = conn.execute(
        """SELECT ca.id, ca.classroom_id, ca.student_id, ca.task_name, ca.deadline, ca.borrowed_task_id
           FROM classroom_assignments ca
           JOIN classroom_members cm ON cm.classroom_id = ca.classroom_id AND cm.student_id = ?
           WHERE ca.id=? AND ca.student_id=?""",
        (uid, assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found or unauthorized.", "danger")
        return redirect(url_for("classrooms"))

    submission_text = request.form.get("submission_text", "").strip()
    submission_file = request.files.get("submission_file")

    existing_sub = conn.execute(
        "SELECT id, file_path, file_name, file_size, file_mime FROM classroom_submissions WHERE assignment_id=? AND student_id=?",
        (assignment_id, uid),
    ).fetchone()

    file_path = existing_sub["file_path"] if existing_sub and existing_sub["file_path"] else ""
    file_name = existing_sub["file_name"] if existing_sub and existing_sub["file_name"] else ""
    file_size = existing_sub["file_size"] if existing_sub and existing_sub["file_size"] else 0
    file_mime = existing_sub["file_mime"] if existing_sub and existing_sub["file_mime"] else ""

    if submission_file and submission_file.filename:
        save_dir = os.path.join(UPLOAD_FOLDER, "classroom_submissions")
        os.makedirs(save_dir, exist_ok=True)
        raw_name = secure_filename(submission_file.filename) or "submission_file"
        unique_name = f"{int(datetime.utcnow().timestamp())}_{secrets.token_hex(6)}_{raw_name}"
        dest = os.path.join(save_dir, unique_name)
        submission_file.save(dest)
        file_path = os.path.join("classroom_submissions", unique_name).replace("\\", "/")
        file_name = submission_file.filename
        file_mime = submission_file.mimetype or "application/octet-stream"
        try:
            file_size = os.path.getsize(dest)
        except Exception:
            file_size = 0

    if not submission_text and not file_path:
        conn.close()
        flash("Please provide either a text response or upload a file for your submission.", "warning")
        return redirect(url_for("classrooms", class_id=assignment["classroom_id"], tab="tab-student-tasks"))

    if existing_sub:
        conn.execute(
            """UPDATE classroom_submissions
               SET submission_text=?, file_path=?, file_name=?, file_size=?, file_mime=?,
                   submitted_at=CURRENT_TIMESTAMP, status='Submitted'
               WHERE id=?""",
            (submission_text, file_path, file_name, file_size, file_mime, existing_sub["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO classroom_submissions
               (assignment_id, classroom_id, student_id, submission_text, file_path, file_name, file_size, file_mime, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Submitted')""",
            (assignment_id, assignment["classroom_id"], uid, submission_text, file_path, file_name, file_size, file_mime),
        )

    conn.execute(
        "UPDATE classroom_assignments SET status='Submitted', started=1 WHERE id=?",
        (assignment_id,),
    )

    if assignment["borrowed_task_id"]:
        conn.execute(
            "UPDATE tasks SET status='Completed' WHERE id=? AND user_id=?",
            (assignment["borrowed_task_id"], uid),
        )

    conn.commit()
    conn.close()

    flash(f'Work for "{assignment["task_name"]}" submitted successfully! 🚀', "success")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"], tab="tab-student-tasks"))


@app.route("/classrooms/submissions/<int:submission_id>/download")
@login_required
def download_classroom_submission(submission_id):
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()
    submission = conn.execute(
        """SELECT s.*, c.faculty_id
           FROM classroom_submissions s
           JOIN classrooms c ON c.id = s.classroom_id
           WHERE s.id=?""",
        (submission_id,),
    ).fetchone()

    if not submission:
        conn.close()
        flash("Submission not found.", "danger")
        return redirect(url_for("classrooms"))

    has_access = False
    if user_type == "faculty" and submission["faculty_id"] == uid:
        has_access = True
    elif user_type == "student" and submission["student_id"] == uid:
        has_access = True

    conn.close()

    if not has_access:
        flash("You do not have permission to download this submission.", "danger")
        return redirect(url_for("classrooms"))

    filepath = submission["file_path"]
    if not filepath or ".." in filepath or filepath.startswith("/") or filepath.startswith("\\"):
        abort(400)

    directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
    filename = os.path.basename(filepath)

    try:
        if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) != os.path.abspath(UPLOAD_FOLDER):
            abort(400)
    except Exception:
        abort(400)

    full_path = os.path.join(directory, filename)
    if not os.path.exists(full_path):
        flash("The requested submission file was not found on the server.", "danger")
        return redirect(url_for("classrooms", class_id=submission["classroom_id"]))

    download_name = submission["file_name"] or filename
    return send_from_directory(
        directory,
        filename,
        as_attachment=True,
        download_name=download_name,
    )


@app.route("/classrooms/submissions/<int:submission_id>/preview")
@login_required
def preview_classroom_submission(submission_id):
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()
    submission = conn.execute(
        """SELECT s.*, c.faculty_id
           FROM classroom_submissions s
           JOIN classrooms c ON c.id = s.classroom_id
           WHERE s.id=?""",
        (submission_id,),
    ).fetchone()

    if not submission:
        conn.close()
        flash("Submission not found.", "danger")
        return redirect(url_for("classrooms"))

    has_access = False
    if user_type == "faculty" and submission["faculty_id"] == uid:
        has_access = True
    elif user_type == "student" and submission["student_id"] == uid:
        has_access = True

    conn.close()

    if not has_access:
        flash("You do not have permission to view this submission.", "danger")
        return redirect(url_for("classrooms"))

    filepath = submission["file_path"]
    if not filepath or ".." in filepath or filepath.startswith("/") or filepath.startswith("\\"):
        abort(400)

    directory = os.path.join(UPLOAD_FOLDER, os.path.dirname(filepath))
    filename = os.path.basename(filepath)

    try:
        if os.path.commonpath([os.path.abspath(directory), os.path.abspath(UPLOAD_FOLDER)]) != os.path.abspath(UPLOAD_FOLDER):
            abort(400)
    except Exception:
        abort(400)

    full_path = os.path.join(directory, filename)
    if not os.path.exists(full_path):
        flash("The requested submission file was not found on the server.", "danger")
        return redirect(url_for("classrooms", class_id=submission["classroom_id"]))

    return send_from_directory(
        directory,
        filename,
        as_attachment=False,
        mimetype=submission["file_mime"] if submission["file_mime"] else None,
    )


@app.route("/classrooms/submissions/<int:submission_id>/grade", methods=["POST"])
@login_required
def grade_classroom_submission(submission_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty can grade submissions.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    grade = request.form.get("grade", "").strip()
    feedback = request.form.get("feedback", "").strip()
    status_input = request.form.get("status", "Reviewed").strip()

    conn = get_db()
    submission = conn.execute(
        """SELECT s.*, c.faculty_id
           FROM classroom_submissions s
           JOIN classrooms c ON c.id = s.classroom_id
           WHERE s.id=?""",
        (submission_id,),
    ).fetchone()

    if not submission or submission["faculty_id"] != uid:
        conn.close()
        flash("Submission not found or unauthorized.", "danger")
        return redirect(url_for("classrooms"))

    conn.execute(
        """UPDATE classroom_submissions
           SET grade=?, feedback=?, status=?, graded_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (grade, feedback, status_input, submission_id),
    )
    conn.execute(
        "UPDATE classroom_assignments SET status=? WHERE id=?",
        (status_input, submission["assignment_id"]),
    )
    conn.commit()
    conn.close()

    flash("Submission evaluation and feedback saved! ✨", "success")
    return redirect(url_for("classrooms", class_id=submission["classroom_id"], tab="tab-tasks"))


@app.route("/classrooms/assignments/<int:assignment_id>/delete", methods=["POST"])
@login_required
def delete_classroom_assignment(assignment_id):
    if session.get("user_type") != "faculty":
        flash("Only faculty can delete assignments.", "danger")
        return redirect(url_for("classrooms"))

    uid = session["user_id"]
    conn = get_db()
    assignment = conn.execute(
        "SELECT id, classroom_id, student_id, attachment_path FROM classroom_assignments WHERE id=? AND faculty_id=?",
        (assignment_id, uid),
    ).fetchone()

    if not assignment:
        conn.close()
        flash("Assignment not found.", "danger")
        return redirect(url_for("classrooms"))

    # Clean up attachment file
    if assignment["attachment_path"]:
        fpath = os.path.join(UPLOAD_FOLDER, assignment["attachment_path"])
        try:
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception:
            pass

    # Clean up submission files
    sub_rows = conn.execute("SELECT file_path FROM classroom_submissions WHERE assignment_id=?", (assignment_id,)).fetchall()
    for s in sub_rows:
        if s["file_path"]:
            fpath = os.path.join(UPLOAD_FOLDER, s["file_path"])
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception:
                pass

    conn.execute("DELETE FROM classroom_submissions WHERE assignment_id=?", (assignment_id,))
    conn.execute(
        "DELETE FROM classroom_assignments WHERE id=? AND faculty_id=?",
        (assignment_id, uid),
    )
    conn.commit()
    conn.close()
    flash("Classroom assignment deleted.", "info")
    return redirect(url_for("classrooms", class_id=assignment["classroom_id"], student_id=assignment["student_id"]))


# ── Subjects ──────────────────────────────────────────────────────────────────
@app.route("/subjects", methods=["GET", "POST"])
@login_required
def subjects():
    uid = session["user_id"]

    if request.method == "POST":
        sname    = request.form.get("subject_name", "").strip()
        diff     = request.form.get("difficulty", "").strip()
        edate    = request.form.get("exam_date", "").strip()
        req_raw  = request.form.get("required_hours", "").strip()
        day_raw  = request.form.get("daily_hours", "").strip()

        if not all([sname, diff, edate, req_raw, day_raw]):
            flash("Please fill all required subject fields.", "warning")
            return redirect(url_for("subjects"))

        if diff not in {"Easy", "Medium", "Hard"}:
            flash("Invalid difficulty selected.", "danger")
            return redirect(url_for("subjects"))

        try:
            req_hrs = float(req_raw)
            day_hrs = float(day_raw)
        except ValueError:
            flash("Please enter valid numeric hours.", "danger")
            return redirect(url_for("subjects"))

        if req_hrs <= 0 or day_hrs <= 0:
            flash("Study hours must be greater than 0.", "warning")
            return redirect(url_for("subjects"))

        try:
            exam_dt = datetime.strptime(edate, "%Y-%m-%d").date()
        except ValueError:
            flash("Please choose a valid exam date.", "danger")
            return redirect(url_for("subjects"))

        if exam_dt < date.today():
            flash("Exam date cannot be in the past.", "warning")
            return redirect(url_for("subjects"))

        # Minimum daily hours needed to finish before exam date.
        days_left = max((exam_dt - date.today()).days, 1)
        min_daily_needed = req_hrs / days_left
        if day_hrs < min_daily_needed:
            day_hrs = round(min_daily_needed, 1)
            flash(
                f"Daily hours auto-adjusted to minimum {day_hrs}h/day based on total hours and exam date.",
                "info",
            )

        conn = get_db()
        conn.execute(
            """INSERT INTO subjects
               (user_id, subject_name, difficulty, exam_date, required_hours, daily_hours)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (uid, sname, diff, edate, req_hrs, day_hrs),
        )

        # Auto-regenerate schedule so new subjects appear immediately in planner graph.
        scheduled_sessions = regenerate_user_schedule(uid, conn)

        conn.commit()
        conn.close()
        flash(f'Subject "{sname}" added!', "success")
        flash(f"Schedule auto-updated with {scheduled_sessions} planned session(s).", "info")
        return redirect(url_for("subjects"))

    conn  = get_db()
    subs  = conn.execute(
        "SELECT * FROM subjects WHERE user_id=? ORDER BY exam_date", (uid,)
    ).fetchall()
    conn.close()

    return render_template(
        "subjects.html",
        subjects=subs,
        today=date.today().strftime("%Y-%m-%d"),
    )


@app.route("/subjects/delete/<int:sid>")
@login_required
def delete_subject(sid):
    conn = get_db()
    conn.execute(
        "DELETE FROM subjects WHERE id=? AND user_id=?", (sid, session["user_id"])
    )

    # Keep schedule synced after subject removal.
    scheduled_sessions = regenerate_user_schedule(session["user_id"], conn)

    conn.commit()
    conn.close()
    flash("Subject removed.", "info")
    flash(f"Schedule re-generated with {scheduled_sessions} session(s).", "info")
    return redirect(url_for("subjects"))


# ── Planner (AI schedule generator) ──────────────────────────────────────────
@app.route("/planner")
@login_required
def planner():
    uid  = session["user_id"]
    conn = get_db()

    subs = conn.execute(
        "SELECT * FROM subjects WHERE user_id=? ORDER BY exam_date", (uid,)
    ).fetchall()

    schedules = conn.execute(
        "SELECT * FROM schedules WHERE user_id=? ORDER BY date", (uid,)
    ).fetchall()

    conn.close()

    subjects_data = [dict(s) for s in subs]
    schedule_rows = [dict(s) for s in schedules]

    # Table rows: include placeholders for subjects that got no schedule slot.
    scheduled_subjects = {row["subject"] for row in schedule_rows}
    schedule_table_rows = []

    for row in schedule_rows:
        row_copy = dict(row)
        row_copy["is_placeholder"] = False
        schedule_table_rows.append(row_copy)

    for sub in subjects_data:
        if sub["subject_name"] not in scheduled_subjects:
            schedule_table_rows.append({
                "id": None,
                "date": "",
                "subject": sub["subject_name"],
                "study_hours": 0,
                "completed": 0,
                "is_placeholder": True,
            })

    # Keep scheduled rows first, then placeholders sorted by subject name.
    scheduled_part = [r for r in schedule_table_rows if not r["is_placeholder"]]
    placeholder_part = sorted(
        [r for r in schedule_table_rows if r["is_placeholder"]],
        key=lambda x: x["subject"].lower(),
    )
    schedule_table_rows = scheduled_part + placeholder_part
    schedule_unallocated_count = len(placeholder_part)

    # Build live priority breakdown for the UI
    priority_breakdown = get_priority_breakdown(subjects_data)

    # Build per-subject schedule breakdown (required hours, deadline, allocated, gap)
    allocated_by_subject = defaultdict(float)
    for row in schedule_rows:
        allocated_by_subject[row["subject"]] += float(row["study_hours"])

    subject_summary = []
    total_required = 0.0
    total_allocated = 0.0

    for sub in subjects_data:
        required_hours = round(float(sub["required_hours"]), 1)
        allocated_hours = round(float(allocated_by_subject.get(sub["subject_name"], 0.0)), 1)
        remaining_hours = round(max(required_hours - allocated_hours, 0.0), 1)

        try:
            days_left = (datetime.strptime(sub["exam_date"], "%Y-%m-%d").date() - date.today()).days
        except Exception:
            days_left = 0

        coverage_pct = round((allocated_hours / required_hours) * 100, 1) if required_hours > 0 else 0

        subject_summary.append({
            "subject": sub["subject_name"],
            "deadline": sub["exam_date"],
            "required_hours": required_hours,
            "allocated_hours": allocated_hours,
            "remaining_hours": remaining_hours,
            "daily_hours": round(float(sub["daily_hours"]), 1),
            "days_left": days_left,
            "coverage_pct": min(coverage_pct, 100.0),
        })

        total_required += required_hours
        total_allocated += allocated_hours

    subject_summary.sort(key=lambda x: x["deadline"])

    # Planner productivity graph: planned vs completed schedule hours (last 7 days)
    planner_productivity = {
        "dates": [],
        "planned": [],
        "completed": [],
        "completion_pct": [],
    }

    for i in range(6, -1, -1):
        d = (date.today() - timedelta(days=i)).strftime("%Y-%m-%d")
        day_rows = [r for r in schedule_rows if r["date"] == d]

        planned_hours = round(sum(float(r["study_hours"]) for r in day_rows), 2)
        completed_hours = round(
            sum(float(r["study_hours"]) for r in day_rows if int(r["completed"]) == 1),
            2,
        )
        completion_pct = round((completed_hours / planned_hours) * 100, 1) if planned_hours > 0 else 0

        planner_productivity["dates"].append(d)
        planner_productivity["planned"].append(planned_hours)
        planner_productivity["completed"].append(completed_hours)
        planner_productivity["completion_pct"].append(completion_pct)

    planner_metrics = {
        "total_required": round(total_required, 1),
        "total_allocated": round(total_allocated, 1),
        "coverage_pct": round((total_allocated / total_required) * 100, 1) if total_required > 0 else 0,
    }

    # Subject-wise generated schedule graph data
    planner_subject_graph = {
        "labels": [row["subject"] for row in subject_summary],
        "required": [row["required_hours"] for row in subject_summary],
        "allocated": [row["allocated_hours"] for row in subject_summary],
    }

    return render_template(
        "planner.html",
        subjects=subs,
        schedules=schedules,
        schedule_table_rows=schedule_table_rows,
        schedule_table_rows_today=[r for r in schedule_table_rows if (safe_get(r, 'date') == date.today().strftime('%Y-%m-%d'))],
        schedule_table_rows_remaining=[r for r in schedule_table_rows if (safe_get(r, 'date') != date.today().strftime('%Y-%m-%d'))],
        schedule_unallocated_count=schedule_unallocated_count,
        priority_breakdown=priority_breakdown,
        subject_summary=subject_summary,
        planner_productivity=planner_productivity,
        planner_metrics=planner_metrics,
        planner_subject_graph=planner_subject_graph,
        today_str=date.today().strftime("%Y-%m-%d"),
        now_time=datetime.now().strftime("%H:%M"),
    )


@app.route("/planner/remaining")
@login_required
def planner_remaining():
    uid = session["user_id"]
    conn = get_db()

    subs = conn.execute(
        "SELECT * FROM subjects WHERE user_id=? ORDER BY exam_date", (uid,)
    ).fetchall()

    schedules = conn.execute(
        "SELECT * FROM schedules WHERE user_id=? ORDER BY date", (uid,)
    ).fetchall()

    conn.close()

    subjects_data = [dict(s) for s in subs]
    schedule_rows = [dict(s) for s in schedules]

    # Build schedule table rows (including placeholders)
    scheduled_subjects = {row["subject"] for row in schedule_rows}
    schedule_table_rows = []
    for row in schedule_rows:
        row_copy = dict(row)
        row_copy["is_placeholder"] = False
        schedule_table_rows.append(row_copy)
    for sub in subjects_data:
        if sub["subject_name"] not in scheduled_subjects:
            schedule_table_rows.append({
                "id": None,
                "date": "",
                "subject": sub["subject_name"],
                "study_hours": 0,
                "completed": 0,
                "is_placeholder": True,
            })

    # Remaining rows = those not for today
    today_str = date.today().strftime("%Y-%m-%d")
    remaining_rows = [r for r in schedule_table_rows if safe_get(r, 'date') != today_str]

    return render_template(
        "remaining_schedule.html",
        subjects=subs,
        schedules=schedules,
        remaining_rows=remaining_rows,
        today_str=today_str,
        now_time=datetime.now().strftime("%H:%M"),
    )


@app.route("/planner/reschedule-missed", methods=["POST"])
@login_required
def planner_reschedule_missed():
    """Manual trigger for missed-session auto-rescheduling."""
    conn = get_db()
    moved = auto_reschedule_missed_sessions(session["user_id"], conn)
    conn.commit()
    conn.close()

    if moved > 0:
        flash(f"📅 Rescheduled {moved} missed study session(s).", "success")
    else:
        flash("No missed sessions to reschedule.", "info")
    return redirect(url_for("planner"))


@app.route("/generate-schedule", methods=["POST"])
@login_required
def generate_schedule_route():
    uid  = session["user_id"]
    conn = get_db()

    subs = conn.execute(
        "SELECT * FROM subjects WHERE user_id=?", (uid,)
    ).fetchall()

    if not subs:
        flash("Add at least one subject before generating a schedule.", "warning")
        conn.close()
        return redirect(url_for("subjects"))

    schedule_count = regenerate_user_schedule(uid, conn)

    conn.commit()
    conn.close()
    flash(f"✅ Schedule generated — {schedule_count} study sessions planned!", "success")
    return redirect(url_for("planner"))


@app.route("/schedule/toggle/<int:sid>", methods=["POST"])
@login_required
def toggle_schedule_status(sid):
    """Toggle a schedule session between Pending and Done."""
    uid = session["user_id"]
    conn = get_db()

    row = conn.execute(
        "SELECT completed, date FROM schedules WHERE id=? AND user_id=?", (sid, uid)
    ).fetchone()

    if row:
        new_state = 0 if int(row["completed"]) == 1 else 1

        # Do not allow marking future sessions as done.
        if new_state == 1:
            try:
                session_date = datetime.strptime(row["date"], "%Y-%m-%d").date()
            except ValueError:
                session_date = date.today()

            if session_date > date.today():
                conn.close()
                flash("You can mark a session as done only on or after its scheduled date.", "warning")
                return redirect(request.referrer or url_for("planner"))

        conn.execute(
            "UPDATE schedules SET completed=? WHERE id=? AND user_id=?",
            (new_state, sid, uid),
        )
        conn.commit()
        flash("Schedule session updated.", "success")

    conn.close()
    return redirect(request.referrer or url_for("planner"))


@app.route("/schedule/time/<int:sid>", methods=["POST"])
@login_required
def update_schedule_time(sid):
    """Save a study time for one schedule row."""
    time_value = request.form.get("scheduled_time", "").strip()

    if time_value:
        try:
            datetime.strptime(time_value, "%H:%M")
        except ValueError:
            flash("Please enter a valid time in HH:MM format.", "warning")
            return redirect(request.referrer or url_for("planner"))

    conn = get_db()
    try:
        conn.execute(
            "UPDATE schedules SET scheduled_time=? WHERE id=? AND user_id=?",
            (time_value or None, sid, session["user_id"]),
        )
        conn.commit()
        flash("Schedule time saved.", "success")
    except sqlite3.Error as e:
        conn.rollback()
        flash(f"Failed to save schedule time: {e}", "danger")
    finally:
        conn.close()

    return redirect(request.referrer or url_for("planner"))


# ── Tasks ─────────────────────────────────────────────────────────────────────
@app.route("/tasks")
@login_required
def tasks():
    uid  = session["user_id"]
    conn = get_db()

    all_tasks = conn.execute(
        "SELECT * FROM tasks WHERE user_id=? ORDER BY deadline", (uid,)
    ).fetchall()

    subject_names = conn.execute(
        "SELECT DISTINCT subject_name FROM subjects WHERE user_id=?", (uid,)
    ).fetchall()

    conn.close()
    return render_template(
        "tasks.html",
        tasks=all_tasks,
        subject_names=subject_names,
        today=date.today().strftime("%Y-%m-%d"),
    )


@app.route("/tasks/add", methods=["POST"])
@login_required
def add_task():
    uid      = session["user_id"]
    tname    = request.form.get("task_name", "").strip()
    subject  = request.form.get("subject", "").strip()
    deadline = request.form.get("deadline", "").strip()

    if not all([tname, subject, deadline]):
        flash("Please fill all required task fields.", "warning")
        return redirect(url_for("tasks"))

    try:
        deadline_dt = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        flash("Please choose a valid deadline date.", "danger")
        return redirect(url_for("tasks"))

    if deadline_dt < date.today():
        flash("Deadline cannot be in the past.", "warning")
        return redirect(url_for("tasks"))

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO tasks (user_id, task_name, subject, deadline, status) VALUES (?,?,?,?,'Pending')",
            (uid, tname, subject, deadline),
        )
        conn.commit()
        conn.close()
        flash("Task added!", "success")
        return redirect(url_for("tasks"))
    except sqlite3.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash(f"Failed to add task: {e}", "danger")
        return redirect(url_for("tasks"))


@app.route("/tasks/edit/<int:tid>", methods=["POST"])
@login_required
def edit_task(tid):
    tname    = request.form.get("task_name", "").strip()
    subject  = request.form.get("subject", "").strip()
    deadline = request.form.get("deadline", "").strip()

    if not all([tname, subject, deadline]):
        flash("Please fill all required task fields.", "warning")
        return redirect(url_for("tasks"))

    try:
        deadline_dt = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        flash("Please choose a valid deadline date.", "danger")
        return redirect(url_for("tasks"))

    if deadline_dt < date.today():
        flash("Deadline cannot be in the past.", "warning")
        return redirect(url_for("tasks"))

    conn = get_db()
    try:
        conn.execute(
            "UPDATE tasks SET task_name=?, subject=?, deadline=? WHERE id=? AND user_id=?",
            (tname, subject, deadline, tid, session["user_id"]),
        )
        conn.commit()
        conn.close()
        flash("Task updated!", "success")
        return redirect(url_for("tasks"))
    except sqlite3.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash(f"Failed to update task: {e}", "danger")
        return redirect(url_for("tasks"))


@app.route("/tasks/delete/<int:tid>")
@login_required
def delete_task(tid):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM tasks WHERE id=? AND user_id=?", (tid, session["user_id"])
        )
        conn.commit()
        conn.close()
        flash("Task deleted.", "info")
        return redirect(url_for("tasks"))
    except sqlite3.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash(f"Failed to delete task: {e}", "danger")
        return redirect(url_for("tasks"))


@app.route("/tasks/complete/<int:tid>")
@login_required
def complete_task(tid):
    conn = get_db()
    try:
        conn.execute(
            "UPDATE tasks SET status='Completed' WHERE id=? AND user_id=?",
            (tid, session["user_id"]),
        )
        conn.commit()
        # Compute updated completion rate for feedback
        try:
            completed = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id=? AND status='Completed'",
                (session["user_id"],),
            ).fetchone()["c"]
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?",
                (session["user_id"],),
            ).fetchone()["c"]
            pct = round(completed / total * 100, 1) if total and total > 0 else 0
            flash(f"🎉 Task completed! Productivity: {pct}% ({completed}/{total} tasks completed)", "success")
        except Exception:
            flash("🎉 Task completed! Great work!", "success")
        finally:
            conn.close()
        return redirect(url_for("tasks"))
    except sqlite3.Error as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        flash(f"Failed to complete task: {e}", "danger")
        return redirect(url_for("tasks"))


@app.route("/manage-data")
@login_required
def manage_data():
    uid = session["user_id"]
    conn = get_db()

    counts = {
        "subjects": conn.execute(
            "SELECT COUNT(*) AS c FROM subjects WHERE user_id=?", (uid,)
        ).fetchone()["c"],
        "tasks": conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE user_id=?", (uid,)
        ).fetchone()["c"],
        "completed_tasks": conn.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE user_id=? AND status='Completed'", (uid,)
        ).fetchone()["c"],
        "study_sessions": conn.execute(
            "SELECT COUNT(*) AS c FROM study_sessions WHERE user_id=?", (uid,)
        ).fetchone()["c"],
        "schedules": conn.execute(
            "SELECT COUNT(*) AS c FROM schedules WHERE user_id=?", (uid,)
        ).fetchone()["c"],
    }

    conn.close()
    return render_template("manage_data.html", counts=counts)


@app.route("/manage-data/action", methods=["POST"])
@login_required
def manage_data_action():
    uid = session["user_id"]
    action = request.form.get("action", "").strip()
    confirm = request.form.get("confirm", "").strip().upper()

    action_queries = {
        "clear_completed_tasks": [
            ("DELETE FROM tasks WHERE user_id=? AND status='Completed'", (uid,)),
        ],
        "clear_tasks": [
            ("DELETE FROM tasks WHERE user_id=?", (uid,)),
        ],
        "clear_sessions": [
            ("DELETE FROM study_sessions WHERE user_id=?", (uid,)),
        ],
        "clear_schedules": [
            ("DELETE FROM schedules WHERE user_id=?", (uid,)),
        ],
        "clear_subjects": [
            ("DELETE FROM schedules WHERE user_id=?", (uid,)),
            ("DELETE FROM subjects WHERE user_id=?", (uid,)),
        ],
        "reset_all": [
            ("DELETE FROM schedules WHERE user_id=?", (uid,)),
            ("DELETE FROM study_sessions WHERE user_id=?", (uid,)),
            ("DELETE FROM tasks WHERE user_id=?", (uid,)),
            ("DELETE FROM subjects WHERE user_id=?", (uid,)),
        ],
    }

    if action not in action_queries:
        flash("Invalid data action.", "danger")
        return redirect(url_for("manage_data"))

    if action == "reset_all" and confirm != "DELETE":
        flash("Type DELETE to confirm full reset.", "warning")
        return redirect(url_for("manage_data"))

    conn = get_db()
    before = conn.total_changes
    for sql, params in action_queries[action]:
        conn.execute(sql, params)
    conn.commit()
    affected = conn.total_changes - before
    conn.close()

    action_names = {
        "clear_completed_tasks": "completed tasks",
        "clear_tasks": "all tasks",
        "clear_sessions": "study sessions",
        "clear_schedules": "study schedules",
        "clear_subjects": "subjects and schedules",
        "reset_all": "all study data",
    }
    flash(f"Deleted {affected} record(s) from {action_names[action]}.", "success")
    return redirect(url_for("manage_data"))


# ── Study session logging (JSON API) ─────────────────────────────────────────
@app.route("/study-session/log", methods=["POST"])
@login_required
def log_study_session():
    data    = request.get_json(silent=True) or {}
    uid     = session["user_id"]
    subject = data.get("subject", "").strip()
    try:
        hours = float(data.get("hours", 0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid hours value."}), 400
    today   = date.today().strftime("%Y-%m-%d")

    if hours > 0 and subject:
        conn = get_db()
        conn.execute(
            "INSERT INTO study_sessions (user_id, subject, hours, date) VALUES (?,?,?,?)",
            (uid, subject, round(hours, 2), today),
        )

        # Feature: auto-mark one matching planned session as completed.
        scheduled = conn.execute(
            """SELECT id FROM schedules
               WHERE user_id=? AND subject=? AND date=? AND completed=0
               ORDER BY id LIMIT 1""",
            (uid, subject, today),
        ).fetchone()

        linked = False
        if scheduled:
            conn.execute(
                "UPDATE schedules SET completed=1 WHERE id=? AND user_id=?",
                (scheduled["id"], uid),
            )
            linked = True

        conn.commit()
        conn.close()
        return jsonify({
            "status": "ok",
            "message": f"Logged {hours:.2f}h for {subject}!",
            "linked_schedule": linked,
        })

    return jsonify({"status": "error", "message": "Invalid data."}), 400


@app.route("/api/subjects-list")
@login_required
def subjects_list():
    """Return a JSON list of subject names for the AI Study Planner timer dropdown."""
    uid  = session["user_id"]
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT subject_name FROM subjects WHERE user_id=?", (uid,)
    ).fetchall()
    conn.close()
    return jsonify([r["subject_name"] for r in rows])


@app.route("/api/schedule-info")
@login_required
def schedule_info():
    """Return the next planned schedule row for a subject."""
    uid = session["user_id"]
    subject = request.args.get("subject", "").strip()

    if not subject:
        return jsonify({"status": "error", "message": "Missing subject."}), 400

    conn = get_db()
    row = conn.execute(
        """
        SELECT date, subject, study_hours, completed, scheduled_time
        FROM schedules
        WHERE user_id=? AND subject=?
        ORDER BY date ASC, CASE WHEN scheduled_time IS NULL OR scheduled_time='' THEN 1 ELSE 0 END, scheduled_time ASC, id ASC
        LIMIT 1
        """,
        (uid, subject),
    ).fetchone()
    conn.close()

    if not row:
        return jsonify({"status": "ok", "found": False})

    return jsonify({
        "status": "ok",
        "found": True,
        "date": row["date"],
        "subject": row["subject"],
        "study_hours": row["study_hours"],
        "completed": int(row["completed"] or 0),
        "scheduled_time": row["scheduled_time"] or "",
    })
def _build_export_dataframe(uid: int, dataset: str, conn):
    """Return a pandas DataFrame for a supported export dataset."""
    if dataset == "sessions":
        return pd.read_sql_query(
            "SELECT date, subject, hours FROM study_sessions WHERE user_id=? ORDER BY date",
            conn,
            params=(uid,),
        )
    if dataset == "tasks":
        return pd.read_sql_query(
            "SELECT task_name, subject, deadline, status, created_at FROM tasks WHERE user_id=? ORDER BY deadline",
            conn,
            params=(uid,),
        )
    if dataset == "schedules":
        return pd.read_sql_query(
            "SELECT date, subject, study_hours, completed FROM schedules WHERE user_id=? ORDER BY date",
            conn,
            params=(uid,),
        )

    return pd.read_sql_query(
        """SELECT date, ROUND(COALESCE(SUM(hours),0),2) AS total_hours
           FROM study_sessions WHERE user_id=?
           GROUP BY date ORDER BY date""",
        conn,
        params=(uid,),
    )


@app.route("/analytics/export/backup/all")
@login_required
def export_backup_zip():
    """Download one ZIP containing all analytics CSV exports."""
    uid = session["user_id"]
    conn = get_db()

    datasets = [
        ("daily", "daily_hours.csv"),
        ("sessions", "study_sessions.csv"),
        ("tasks", "tasks.csv"),
        ("schedules", "schedules.csv"),
    ]

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dataset, filename in datasets:
            df = _build_export_dataframe(uid, dataset, conn)
            zf.writestr(filename, df.to_csv(index=False))

    conn.close()
    zip_buffer.seek(0)

    response = make_response(zip_buffer.getvalue())
    response.headers["Content-Disposition"] = (
        f"attachment; filename=backup_user_{uid}_{date.today().strftime('%Y%m%d')}.zip"
    )
    response.mimetype = "application/zip"
    return response


@app.route("/analytics/export/<string:dataset>")
@login_required
def export_analytics_csv(dataset):
    """Download analytics-related user data as CSV (sessions/tasks/schedules/daily)."""
    uid = session["user_id"]
    allowed = {"sessions", "tasks", "schedules", "daily"}
    if dataset not in allowed:
        return jsonify({"status": "error", "message": "Invalid dataset"}), 400

    conn = get_db()
    df = _build_export_dataframe(uid, dataset, conn)
    conn.close()

    csv_data = df.to_csv(index=False)
    response = make_response(csv_data)
    response.headers["Content-Disposition"] = (
        f"attachment; filename={dataset}_user_{uid}_{date.today().strftime('%Y%m%d')}.csv"
    )
    response.mimetype = "text/csv"
    return response


@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    uid = session['user_id']
    conn = get_db()
    if request.method == 'POST':
        try:
            val = float(request.form.get('daily_limit', '6').strip())
            if val < 0:
                raise ValueError('Must be non-negative')
        except Exception:
            flash('Please enter a valid number for daily hours.', 'danger')
            conn.close()
            return redirect(url_for('settings'))

        # Read timer settings (integers)
        try:
            fmins = int(request.form.get('focus_minutes', '').strip() or 25)
            smins = int(request.form.get('short_minutes', '').strip() or 5)
            lmins = int(request.form.get('long_minutes', '').strip() or 15)
            sbefore = int(request.form.get('sessions_before_long', '').strip() or 4)
            if fmins < 1 or smins < 1 or lmins < 1 or sbefore < 1:
                raise ValueError('Values must be >= 1')
        except Exception:
            flash('Please enter valid integer values for timer minutes/sessions.', 'danger')
            conn.close()
            return redirect(url_for('settings'))

        try:
            conn.execute(
                'UPDATE users SET daily_hours_allowed = ?, timer_focus = ?, timer_short = ?, timer_long = ?, timer_sessions_before_long = ? WHERE id = ?',
                (val, fmins, smins, lmins, sbefore, uid),
            )
            conn.commit()
            flash('Settings saved.', 'success')
        except sqlite3.Error as e:
            flash(f'Failed to save settings: {e}', 'danger')
        finally:
            conn.close()
        return redirect(url_for('settings'))

    # GET - show current values (daily + timer)
    try:
        row = conn.execute(
            'SELECT COALESCE(daily_hours_allowed,6.0) AS daily, COALESCE(timer_focus,25) AS tf, COALESCE(timer_short,5) AS ts, COALESCE(timer_long,15) AS tl, COALESCE(timer_sessions_before_long,4) AS tss FROM users WHERE id=?',
            (uid,),
        ).fetchone()
        if row:
            daily_limit = round(float(row['daily']), 1)
            tf = int(row['tf'])
            ts = int(row['ts'])
            tl = int(row['tl'])
            tss = int(row['tss'])
        else:
            daily_limit, tf, ts, tl, tss = 6.0, 25, 5, 15, 4
    except Exception:
        daily_limit, tf, ts, tl, tss = 6.0, 25, 5, 15, 4
    created_classrooms = []
    classrooms_with_students = []
    try:
        if session.get('user_type') == 'faculty':
            created_classrooms = conn.execute(
                "SELECT id, class_name, class_code, created_at FROM classrooms WHERE faculty_id=? ORDER BY created_at DESC",
                (uid,),
            ).fetchall()

            for c in created_classrooms:
                st_list = conn.execute(
                    """SELECT u.id, u.name, u.email, cm.joined_at
                       FROM classroom_members cm
                       JOIN users u ON u.id = cm.student_id
                       WHERE cm.classroom_id=?
                       ORDER BY u.name COLLATE NOCASE""",
                    (c["id"],),
                ).fetchall()
                classrooms_with_students.append({
                    "id": c["id"],
                    "class_name": c["class_name"],
                    "class_code": c["class_code"],
                    "students": st_list,
                })
    except Exception:
        created_classrooms = []
        classrooms_with_students = []
    finally:
        conn.close()

    return render_template(
        'settings.html',
        daily_limit=daily_limit,
        timer_focus=tf,
        timer_short=ts,
        timer_long=tl,
        timer_sessions_before_long=tss,
        created_classrooms=created_classrooms,
        classrooms_with_students=classrooms_with_students,
        user_type=session.get('user_type', 'student'),
    )


@app.route('/forgot', methods=['GET', 'POST'])
def forgot():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        if not email:
            flash('Enter your email.', 'danger')
            return redirect(url_for('forgot'))
        conn = get_db()
        user = conn.execute('SELECT id, email, name FROM users WHERE email=?', (email,)).fetchone()
        if not user:
            flash('If that email exists, a reset link has been sent.', 'info')
            return redirect(url_for('login'))

        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)', (user['id'], token, expires))
        conn.commit()
        conn.close()

        reset_link = url_for('reset_password', token=token, _external=True)

        # Try to send email if SMTP configured
        smtp_host = os.environ.get('SMTP_HOST')
        smtp_port = int(os.environ.get('SMTP_PORT', '0') or 0)
        smtp_user = os.environ.get('SMTP_USER')
        smtp_pass = os.environ.get('SMTP_PASS')
        from_addr = os.environ.get('FROM_EMAIL', smtp_user)
        try:
            if smtp_host and smtp_port and smtp_user and smtp_pass and from_addr:
                msg = EmailMessage()
                msg['Subject'] = 'AI Study Planner — Password reset'
                msg['From'] = from_addr
                msg['To'] = user['email']
                msg.set_content(f"Hi {user['name']},\n\nUse this link to reset your password (expires in 1 hour):\n{reset_link}\n\nIf you didn't request this, ignore.\n")
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as s:
                    s.starttls()
                    s.login(smtp_user, smtp_pass)
                    s.send_message(msg)
                flash('Reset link sent to your email.', 'success')
            else:
                # SMTP not configured — show link in flash (development)
                flash(f'Reset link (dev): {reset_link}', 'info')
        except Exception:
            flash(f'Reset link (dev): {reset_link}', 'info')

        return redirect(url_for('login'))

    return render_template('forgot.html')


@app.route('/reset/<token>', methods=['GET', 'POST'])
def reset_password(token):
    conn = get_db()
    row = conn.execute('SELECT * FROM password_resets WHERE token=?', (token,)).fetchone()
    if not row:
        flash('Invalid or expired reset token.', 'danger')
        return redirect(url_for('login'))

    # check expiry
    expires = datetime.strptime(row['expires_at'], '%Y-%m-%d %H:%M:%S')
    if datetime.utcnow() > expires:
        conn.execute('DELETE FROM password_resets WHERE id=?', (row['id'],))
        conn.commit()
        conn.close()
        flash('Reset token expired.', 'danger')
        return redirect(url_for('forgot'))

    if request.method == 'POST':
        new = request.form.get('new_password', '')
        confirm = request.form.get('confirm_password', '')
        if not new or len(new) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return redirect(url_for('reset_password', token=token))
        if new != confirm:
            flash('Passwords do not match.', 'danger')
            return redirect(url_for('reset_password', token=token))

        # update password
        conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(new), row['user_id']))
        conn.execute('DELETE FROM password_resets WHERE id=?', (row['id'],))
        conn.commit()
        conn.close()
        flash('Password updated. Please log in.', 'success')
        return redirect(url_for('login'))

    conn.close()
    return render_template('reset_password.html')


@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    uid = session['user_id']
    conn = get_db()

    if request.method == 'POST':
        # Distinguish form actions by hidden field 'action'
        action = request.form.get('action', '')
        if action == 'update_profile':
            name = request.form.get('name', '').strip()
            class_name = request.form.get('class_name', '').strip()
            age = request.form.get('age', '').strip()
            daily_hours = request.form.get('daily_hours')
            receive_emails = 1 if request.form.get('receive_emails') == 'on' else 0
            try:
                age_val = int(age) if age else None
            except ValueError:
                age_val = None

            try:
                daily_hours_val = float(daily_hours) if daily_hours else None
            except Exception:
                daily_hours_val = None

            # Handle avatar upload
            avatar_file = request.files.get('avatar')
            avatar_filename = None
            if avatar_file and avatar_file.filename:
                fname = secure_filename(avatar_file.filename)
                avatar_filename = f"{uid}_{secrets.token_hex(8)}_{fname}"
                avatar_path = os.path.join(UPLOAD_FOLDER, avatar_filename)
                avatar_file.save(avatar_path)

            if not name:
                flash('Name cannot be empty.', 'danger')
                return redirect(url_for('profile'))

            # Build update tuple
            if avatar_filename:
                conn.execute("UPDATE users SET name=?, class_name=?, age=?, avatar=?, receive_emails=?, daily_hours_allowed=? WHERE id=?",
                             (name, class_name, age_val, avatar_filename, receive_emails, daily_hours_val, uid))
                session['user_avatar'] = avatar_filename
            else:
                conn.execute("UPDATE users SET name=?, class_name=?, age=?, receive_emails=?, daily_hours_allowed=? WHERE id=?",
                             (name, class_name, age_val, receive_emails, daily_hours_val, uid))
            conn.commit()
            session['user_name'] = name
            flash('Profile updated.', 'success')
            return redirect(url_for('profile'))

        if action == 'change_password':
            current = request.form.get('current_password', '')
            new = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')
            if not new or len(new) < 6:
                flash('New password must be at least 6 characters.', 'danger')
                return redirect(url_for('profile'))
            if new != confirm:
                flash('New password and confirmation do not match.', 'danger')
                return redirect(url_for('profile'))

            user = conn.execute('SELECT password FROM users WHERE id=?', (uid,)).fetchone()
            if not user or not check_password_hash(user['password'], current):
                flash('Current password is incorrect.', 'danger')
                return redirect(url_for('profile'))

            conn.execute('UPDATE users SET password=? WHERE id=?', (generate_password_hash(new), uid))
            conn.commit()
            flash('Password changed.', 'success')
            return redirect(url_for('profile'))

    # GET: render profile form
    row = conn.execute('SELECT id, name, email, class_name, age, avatar, receive_emails, daily_hours_allowed FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    user = dict(row) if row else {}
    return render_template('profile.html', user=user)


@app.route("/quiz", methods=["GET", "POST"])
@login_required
def quiz():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()

    # Shared / Student PDF practice vars
    quiz_questions = None
    quiz_results = None
    score = None
    total = 0
    source_name = None
    attempt_id = None
    quiz_history = _fetch_quiz_attempts(uid)

    # Faculty-specific vars
    faculty_quizzes = []
    faculty_classrooms = []
    selected_quiz_results = None
    selected_quiz = None

    # Student live quiz vars
    active_live_quizzes = []
    student_live_submissions = []

    # Handle PDF generation / grading POST if submitted via legacy PDF form
    if request.method == "POST":
        action = request.form.get("action", "generate")
        quiz_draft = session.get("quiz_draft") or {}

        if action == "generate":
            pdf_file = request.files.get("quiz_pdf")
            if not pdf_file or not pdf_file.filename:
                flash("Please choose a PDF file to generate a quiz.", "warning")
            elif not pdf_file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are supported.", "danger")
            else:
                try:
                    extracted_text = _extract_pdf_text(pdf_file)
                    quiz_questions = _build_quiz_questions(extracted_text)
                    source_name = secure_filename(pdf_file.filename)
                    if not quiz_questions:
                        flash("No quiz questions could be generated from that PDF. Try a more text-heavy file.", "warning")
                    else:
                        session["quiz_draft"] = {
                            "source_name": source_name,
                            "quiz_questions": quiz_questions,
                        }
                        flash(f"Generated {len(quiz_questions)} quiz question(s) from {source_name}.", "success")
                except Exception as exc:
                    flash(f"Could not read the PDF: {exc}", "danger")

        elif action == "grade":
            quiz_data_raw = request.form.get("quiz_data", "[]")
            source_name = request.form.get("source_name", "").strip() or quiz_draft.get("source_name")
            try:
                quiz_questions = json.loads(quiz_data_raw)
            except json.JSONDecodeError:
                quiz_questions = quiz_draft.get("quiz_questions")

            if not isinstance(quiz_questions, list) or not quiz_questions:
                quiz_questions = quiz_draft.get("quiz_questions")

            if isinstance(quiz_questions, list) and quiz_questions:
                quiz_results = []
                correct_count = 0
                total = len(quiz_questions)

                for index, question in enumerate(quiz_questions):
                    selected = request.form.get(f"answer_{index}", "")
                    correct_answer = str(question.get("answer", "")).strip()
                    is_correct = selected.strip().lower() == correct_answer.lower()
                    if is_correct:
                        correct_count += 1

                    quiz_results.append({
                        "question": question.get("question", ""),
                        "selected": selected,
                        "correct": correct_answer,
                        "is_correct": is_correct,
                    })

                score = round((correct_count / total) * 100) if total else 0
                quiz_results_payload = json.dumps(quiz_results)
                quiz_data_payload = json.dumps(quiz_questions)

                cursor = conn.execute(
                    """
                    INSERT INTO quiz_attempts (
                        user_id, source_name, total_questions, correct_answers, score,
                        quiz_data, quiz_results
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        uid,
                        source_name,
                        total,
                        correct_count,
                        score,
                        quiz_data_payload,
                        quiz_results_payload,
                    ),
                )
                attempt_id = cursor.lastrowid
                conn.commit()
                quiz_history = _fetch_quiz_attempts(uid)
                session.pop("quiz_draft", None)
                flash(f"You scored {correct_count}/{total} ({score}%).", "info")

        elif action == "export":
            source_name = request.form.get("source_name", "").strip() or quiz_draft.get("source_name") or "Generated Quiz"
            quiz_data_raw = request.form.get("quiz_data", "[]")
            try:
                quiz_questions = json.loads(quiz_data_raw)
            except json.JSONDecodeError:
                quiz_questions = quiz_draft.get("quiz_questions")

            if isinstance(quiz_questions, list) and quiz_questions:
                pdf_bytes = _build_quiz_txt_content(
                    title="AI Study Planner Quiz Export",
                    quiz_questions=quiz_questions,
                    source_name=source_name,
                )
                filename = f"quiz_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
                conn.close()
                return _quiz_txt_response(filename, pdf_bytes)

    # Populate view data based on role
    if user_type == "faculty":
        faculty_classrooms = conn.execute(
            "SELECT id, class_name, class_code FROM classrooms WHERE faculty_id = ? ORDER BY class_name ASC",
            (uid,),
        ).fetchall()

        faculty_quizzes_rows = conn.execute(
            """
            SELECT q.id, q.faculty_id, q.classroom_id, q.title, q.subject, q.description,
                   q.duration_minutes, q.status, q.quiz_data, q.started_at, q.created_at,
                   c.class_name,
                   (SELECT COUNT(*) FROM quiz_submissions qs WHERE qs.quiz_id = q.id) as submissions_count,
                   (SELECT ROUND(AVG(qs.score), 1) FROM quiz_submissions qs WHERE qs.quiz_id = q.id) as avg_score,
                   (SELECT MAX(qs.score) FROM quiz_submissions qs WHERE qs.quiz_id = q.id) as max_score
            FROM quizzes q
            LEFT JOIN classrooms c ON q.classroom_id = c.id
            WHERE q.faculty_id = ?
            ORDER BY q.id DESC
            """,
            (uid,),
        ).fetchall()
        faculty_quizzes = [dict(row) for row in faculty_quizzes_rows]
        for fq in faculty_quizzes:
            try:
                data_list = json.loads(fq["quiz_data"])
                fq["question_count"] = len(data_list) if isinstance(data_list, list) else 0
            except Exception:
                fq["question_count"] = 0

        # Check if a specific quiz results view was requested
        view_quiz_id = request.args.get("view_quiz") or request.args.get("view_results")
        if view_quiz_id:
            try:
                vq_id = int(view_quiz_id)
                q_row = conn.execute(
                    """SELECT q.*, c.class_name FROM quizzes q
                       LEFT JOIN classrooms c ON q.classroom_id = c.id
                       WHERE q.id = ? AND q.faculty_id = ?""",
                    (vq_id, uid),
                ).fetchone()
                if q_row:
                    selected_quiz = dict(q_row)
                    try:
                        selected_quiz["parsed_data"] = json.loads(selected_quiz["quiz_data"])
                    except Exception:
                        selected_quiz["parsed_data"] = []

                    subs = conn.execute(
                        """SELECT qs.*, u.name as student_name, u.email as student_email, u.avatar
                           FROM quiz_submissions qs
                           JOIN users u ON qs.student_id = u.id
                           WHERE qs.quiz_id = ?
                           ORDER BY qs.score DESC, qs.submitted_at ASC""",
                        (vq_id,),
                    ).fetchall()
                    selected_quiz_results = [dict(s) for s in subs]
                    quiz_questions_map = {}
                    if selected_quiz and isinstance(selected_quiz.get("parsed_data"), list):
                        for q_idx_item, q_item in enumerate(selected_quiz["parsed_data"]):
                            quiz_questions_map[q_idx_item] = q_item

                    for s in selected_quiz_results:
                        try:
                            s["parsed_results"] = json.loads(s["results_json"])
                        except Exception:
                            s["parsed_results"] = []

                        # If parsed_results is empty or missing, reconstruct from answers_json & quiz_data
                        if not s["parsed_results"] and s.get("answers_json"):
                            try:
                                ans_map = json.loads(s["answers_json"])
                                reconstructed = []
                                for q_idx_item, q_item in enumerate(selected_quiz.get("parsed_data", [])):
                                    sel_raw = str(ans_map.get(str(q_idx_item), "")).strip()
                                    corr_raw = str(q_item.get("answer", "")).strip()
                                    opts = q_item.get("options", [])
                                    sel = opts[int(sel_raw)] if (sel_raw.isdigit() and int(sel_raw) < len(opts)) else sel_raw
                                    corr = opts[int(corr_raw)] if (corr_raw.isdigit() and int(corr_raw) < len(opts)) else corr_raw
                                    is_c = (sel.lower() == corr.lower())
                                    reconstructed.append({
                                        "question": q_item.get("question", ""),
                                        "selected": sel,
                                        "correct": corr,
                                        "is_correct": is_c,
                                        "options": opts,
                                    })
                                s["parsed_results"] = reconstructed
                            except Exception:
                                pass

                        # Ensure every result item has options list populated
                        for r_idx, r_item in enumerate(s["parsed_results"]):
                            if not r_item.get("options") and r_idx in quiz_questions_map:
                                r_item["options"] = quiz_questions_map[r_idx].get("options", [])
            except Exception as e:
                pass

    else:
        # Student View: active live quizzes & completed results
        live_rows = conn.execute(
            """
            SELECT q.id, q.faculty_id, q.classroom_id, q.title, q.subject, q.description,
                   q.duration_minutes, q.status, q.started_at, q.created_at, q.quiz_data,
                   c.class_name, u.name as faculty_name,
                   (SELECT qs.score FROM quiz_submissions qs WHERE qs.quiz_id = q.id AND qs.student_id = ?) as my_score,
                   (SELECT qs.id FROM quiz_submissions qs WHERE qs.quiz_id = q.id AND qs.student_id = ?) as my_submission_id
            FROM quizzes q
            JOIN users u ON q.faculty_id = u.id
            LEFT JOIN classrooms c ON q.classroom_id = c.id
            WHERE q.status = 'active'
              AND (q.classroom_id IS NULL OR q.classroom_id IN (
                  SELECT classroom_id FROM classroom_members WHERE student_id = ?
              ))
            ORDER BY q.id DESC
            """,
            (uid, uid, uid),
        ).fetchall()
        active_live_quizzes = [dict(r) for r in live_rows]
        for alq in active_live_quizzes:
            try:
                data_list = json.loads(alq["quiz_data"])
                alq["question_count"] = len(data_list) if isinstance(data_list, list) else 0
            except Exception:
                alq["question_count"] = 0

        sub_rows = conn.execute(
            """
            SELECT qs.id, qs.quiz_id, qs.score, qs.correct_answers, qs.total_questions,
                   qs.submitted_at, qs.results_json,
                   q.title as quiz_title, q.subject, c.class_name, u.name as faculty_name
            FROM quiz_submissions qs
            JOIN quizzes q ON qs.quiz_id = q.id
            JOIN users u ON q.faculty_id = u.id
            LEFT JOIN classrooms c ON q.classroom_id = c.id
            WHERE qs.student_id = ?
            ORDER BY qs.id DESC
            """,
            (uid,),
        ).fetchall()
        student_live_submissions = [dict(r) for r in sub_rows]
        for s in student_live_submissions:
            try:
                s["parsed_results"] = json.loads(s["results_json"])
            except Exception:
                s["parsed_results"] = []

    conn.close()

    return render_template(
        "quiz.html",
        user_type=user_type,
        is_faculty=(user_type == "faculty"),
        faculty_quizzes=faculty_quizzes,
        faculty_classrooms=faculty_classrooms,
        selected_quiz=selected_quiz,
        selected_quiz_results=selected_quiz_results,
        active_live_quizzes=active_live_quizzes,
        student_live_submissions=student_live_submissions,
        quiz_history=quiz_history,
        quiz_questions=quiz_questions or [],
        source_name=source_name or "",
        quiz_results=quiz_results,
        score=score,
        total=total,
        attempt_id=attempt_id,
    )


@app.route("/quiz/create", methods=["POST"])
@login_required
def quiz_create():
    uid = session["user_id"]
    if session.get("user_type") != "faculty":
        return jsonify({"status": "error", "message": "Only faculty members can create quizzes."}), 403

    is_json = request.is_json
    data = request.get_json(silent=True) if is_json else request.form

    title = (data.get("title") or "").strip()
    if not title:
        if is_json:
            return jsonify({"status": "error", "message": "Quiz title is required."}), 400
        flash("Quiz title is required.", "danger")
        return redirect(url_for("quiz"))

    subject = (data.get("subject") or "").strip()
    description = (data.get("description") or "").strip()
    raw_class_id = data.get("classroom_id")
    classroom_id = int(raw_class_id) if raw_class_id and str(raw_class_id).isdigit() else None
    
    try:
        duration_minutes = int(data.get("duration_minutes") or 0)
    except (ValueError, TypeError):
        duration_minutes = 0

    status = (data.get("status") or "draft").strip().lower()
    if status not in ("draft", "active", "ended"):
        status = "draft"

    # Extract questions
    questions = []
    if is_json:
        raw_questions = data.get("questions") or []
        if isinstance(raw_questions, list):
            for rq in raw_questions:
                q_text = str(rq.get("question", "")).strip()
                opts = [str(o).strip() for o in rq.get("options", []) if str(o).strip()]
                ans_raw = str(rq.get("answer", "")).strip()
                if ans_raw.isdigit() and int(ans_raw) < len(opts):
                    ans = opts[int(ans_raw)]
                elif ans_raw in opts:
                    ans = ans_raw
                elif opts:
                    ans = opts[0]
                else:
                    ans = ""
                if q_text and opts:
                    questions.append({
                        "question": q_text,
                        "options": opts,
                        "answer": ans,
                    })
    else:
        raw_json = data.get("quiz_data_json")
        if raw_json:
            try:
                parsed_json = json.loads(raw_json)
                if isinstance(parsed_json, list):
                    for rq in parsed_json:
                        q_text = str(rq.get("question", "")).strip()
                        opts = [str(o).strip() for o in rq.get("options", []) if str(o).strip()]
                        ans_raw = str(rq.get("answer", "")).strip()
                        if ans_raw.isdigit() and int(ans_raw) < len(opts):
                            ans = opts[int(ans_raw)]
                        elif ans_raw in opts:
                            ans = ans_raw
                        elif opts:
                            ans = opts[0]
                        else:
                            ans = ""
                        if q_text and opts:
                            questions.append({
                                "question": q_text,
                                "options": opts,
                                "answer": ans,
                            })
            except Exception:
                questions = []

        if not questions:
            # Parse from form fields (q_text_0, q_opt_0_0, etc.)
            q_idx = 0
            while f"q_text_{q_idx}" in request.form:
                q_text = request.form.get(f"q_text_{q_idx}", "").strip()
                if q_text:
                    opts = []
                    for opt_idx in range(4):
                        opt_val = request.form.get(f"q_opt_{q_idx}_{opt_idx}", "").strip()
                        if opt_val:
                            opts.append(opt_val)
                    ans_raw = request.form.get(f"q_ans_{q_idx}", "").strip()
                    if ans_raw.isdigit() and int(ans_raw) < len(opts):
                        ans = opts[int(ans_raw)]
                    elif ans_raw in opts:
                        ans = ans_raw
                    elif opts:
                        ans = opts[0]
                    else:
                        ans = ""
                    questions.append({
                        "question": q_text,
                        "options": opts,
                        "answer": ans,
                    })
                q_idx += 1

    if not questions:
        if is_json:
            return jsonify({"status": "error", "message": "Please add at least one question to the quiz."}), 400
        flash("Please add at least one question to the quiz.", "warning")
        return redirect(url_for("quiz"))

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if status == "active" else None

    conn = get_db()
    cursor = conn.execute(
        """
        INSERT INTO quizzes (
            faculty_id, classroom_id, title, subject, description,
            duration_minutes, status, quiz_data, started_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            classroom_id,
            title,
            subject,
            description,
            duration_minutes,
            status,
            json.dumps(questions),
            started_at,
        ),
    )
    quiz_id = cursor.lastrowid
    conn.commit()
    conn.close()

    if is_json:
        return jsonify({
            "status": "ok",
            "message": "Quiz created successfully." if status != "active" else "Live quiz started and published!",
            "quiz_id": quiz_id,
        })

    msg = f"Quiz \"{title}\" created and started live for students!" if status == "active" else f"Quiz \"{title}\" saved as draft."
    flash(msg, "success")
    return redirect(url_for("quiz"))


@app.route("/quiz/<int:quiz_id>/status", methods=["POST"])
@login_required
def quiz_toggle_status(quiz_id):
    uid = session["user_id"]
    if session.get("user_type") != "faculty":
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    data = request.get_json(silent=True) if request.is_json else request.form
    new_status = (data.get("status") or "active").strip().lower()
    if new_status not in ("draft", "active", "ended"):
        new_status = "active"

    conn = get_db()
    quiz_row = conn.execute("SELECT id, title FROM quizzes WHERE id = ? AND faculty_id = ?", (quiz_id, uid)).fetchone()
    if not quiz_row:
        conn.close()
        return jsonify({"status": "error", "message": "Quiz not found"}), 404

    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S") if new_status == "active" else None
    if started_at:
        conn.execute("UPDATE quizzes SET status = ?, started_at = ? WHERE id = ?", (new_status, started_at, quiz_id))
    else:
        conn.execute("UPDATE quizzes SET status = ? WHERE id = ?", (new_status, quiz_id))
    conn.commit()
    conn.close()

    if request.is_json:
        return jsonify({
            "status": "ok",
            "new_status": new_status,
            "message": f"Quiz is now {new_status.capitalize()}.",
        })

    flash(f"Quiz \"{quiz_row['title']}\" status updated to {new_status.capitalize()}.", "info")
    return redirect(url_for("quiz"))


@app.route("/quiz/<int:quiz_id>/delete", methods=["POST"])
@login_required
def quiz_delete(quiz_id):
    uid = session["user_id"]
    if session.get("user_type") != "faculty":
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    conn = get_db()
    quiz_row = conn.execute("SELECT id, title FROM quizzes WHERE id = ? AND faculty_id = ?", (quiz_id, uid)).fetchone()
    if not quiz_row:
        conn.close()
        flash("Quiz not found.", "danger")
        return redirect(url_for("quiz"))

    conn.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    conn.commit()
    conn.close()

    flash(f"Quiz \"{quiz_row['title']}\" has been deleted.", "success")
    return redirect(url_for("quiz"))


@app.route("/quiz/take/<int:quiz_id>", methods=["GET"])
@login_required
def quiz_take(quiz_id):
    uid = session["user_id"]
    conn = get_db()

    quiz_row = conn.execute(
        """SELECT q.*, c.class_name, u.name as faculty_name
           FROM quizzes q
           JOIN users u ON q.faculty_id = u.id
           LEFT JOIN classrooms c ON q.classroom_id = c.id
           WHERE q.id = ?""",
        (quiz_id,),
    ).fetchone()

    if not quiz_row:
        conn.close()
        flash("Quiz not found or has been removed.", "danger")
        return redirect(url_for("quiz"))

    quiz_data = dict(quiz_row)
    try:
        raw_questions = json.loads(quiz_data["quiz_data"])
    except Exception:
        raw_questions = []

    # Check if student already submitted this quiz
    existing_sub = conn.execute(
        "SELECT * FROM quiz_submissions WHERE quiz_id = ? AND student_id = ?",
        (quiz_id, uid),
    ).fetchone()

    conn.close()

    sub_dict = None
    if existing_sub:
        sub_dict = dict(existing_sub)
        try:
            sub_dict["parsed_results"] = json.loads(sub_dict["results_json"])
        except Exception:
            sub_dict["parsed_results"] = []

    # Strip answers for students taking the active quiz to prevent inspect element lookup
    safe_questions = []
    for idx, q in enumerate(raw_questions):
        safe_questions.append({
            "id": idx,
            "question": q.get("question", ""),
            "options": q.get("options", []),
        })

    return render_template(
        "quiz_take.html",
        quiz=quiz_data,
        questions=safe_questions,
        total_questions=len(safe_questions),
        existing_submission=sub_dict,
    )


@app.route("/quiz/submit/<int:quiz_id>", methods=["POST"])
@login_required
def quiz_submit_live(quiz_id):
    uid = session["user_id"]
    conn = get_db()

    quiz_row = conn.execute(
        """SELECT q.*, c.class_name, u.name as faculty_name
           FROM quizzes q
           JOIN users u ON q.faculty_id = u.id
           LEFT JOIN classrooms c ON q.classroom_id = c.id
           WHERE q.id = ?""",
        (quiz_id,),
    ).fetchone()

    if not quiz_row:
        conn.close()
        if request.is_json:
            return jsonify({"status": "error", "message": "Quiz not found"}), 404
        flash("Quiz not found.", "danger")
        return redirect(url_for("quiz"))

    if quiz_row["status"] != "active":
        conn.close()
        if request.is_json:
            return jsonify({"status": "error", "message": "This quiz is no longer active."}), 400
        flash("This quiz is no longer accepting submissions.", "warning")
        return redirect(url_for("quiz"))

    try:
        stored_questions = json.loads(quiz_row["quiz_data"])
    except Exception:
        stored_questions = []

    is_json = request.is_json
    answers_map = {}
    if is_json:
        data = request.get_json(silent=True) or {}
        answers_map = data.get("answers", {})
    else:
        for idx in range(len(stored_questions)):
            val = request.form.get(f"answer_{idx}", "")
            answers_map[str(idx)] = val

    # Evaluate answers server-side
    correct_count = 0
    total = len(stored_questions)
    results = []

    for idx, q in enumerate(stored_questions):
        selected_raw = str(answers_map.get(str(idx), "")).strip()
        correct_raw = str(q.get("answer", "")).strip()
        opts = q.get("options", [])

        # Map indices to option strings if needed
        selected = selected_raw
        if selected_raw.isdigit() and int(selected_raw) < len(opts):
            selected = opts[int(selected_raw)]

        correct = correct_raw
        if correct_raw.isdigit() and int(correct_raw) < len(opts):
            correct = opts[int(correct_raw)]

        is_correct = (selected.strip().lower() == correct.strip().lower())
        if is_correct:
            correct_count += 1

        results.append({
            "question": q.get("question", ""),
            "selected": selected,
            "correct": correct,
            "is_correct": is_correct,
            "options": opts,
        })

    score = round((correct_count / total) * 100) if total else 0

    student_row = conn.execute("SELECT name, email FROM users WHERE id = ?", (uid,)).fetchone()
    student_name = student_row["name"] if student_row else session.get("user_name", "Student")
    student_email = student_row["email"] if student_row else session.get("user_email", "")

    # Save to quiz_submissions table (multi-student concurrency safe)
    cursor = conn.execute(
        """
        INSERT INTO quiz_submissions (
            quiz_id, student_id, student_name, student_email, classroom_id,
            score, correct_answers, total_questions, answers_json, results_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            quiz_id,
            uid,
            student_name,
            student_email,
            quiz_row["classroom_id"],
            score,
            correct_count,
            total,
            json.dumps(answers_map),
            json.dumps(results),
        ),
    )
    submission_id = cursor.lastrowid

    # Also log to quiz_attempts for unified student analytics
    conn.execute(
        """
        INSERT INTO quiz_attempts (
            user_id, source_name, total_questions, correct_answers, score,
            quiz_data, quiz_results
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            f"Live Quiz: {quiz_row['title']}",
            total,
            correct_count,
            score,
            quiz_row["quiz_data"],
            json.dumps(results),
        ),
    )
    conn.commit()
    conn.close()

    if is_json:
        return jsonify({
            "status": "ok",
            "submission_id": submission_id,
            "score": score,
            "correct_answers": correct_count,
            "total_questions": total,
            "results": results,
            "message": f"Quiz submitted successfully! Your score: {score}%",
        })

    flash(f"Quiz submitted! You scored {correct_count}/{total} ({score}%).", "success")
    return redirect(url_for("quiz_take", quiz_id=quiz_id))


@app.route("/api/quiz/<int:quiz_id>/results", methods=["GET"])
@login_required
def api_quiz_results(quiz_id):
    uid = session["user_id"]
    conn = get_db()

    quiz_row = conn.execute(
        "SELECT * FROM quizzes WHERE id = ? AND faculty_id = ?",
        (quiz_id, uid),
    ).fetchone()

    if not quiz_row:
        conn.close()
        return jsonify({"status": "error", "message": "Quiz not found or unauthorized"}), 404

    subs = conn.execute(
        """
        SELECT qs.id, qs.student_id, qs.student_name, qs.student_email,
               qs.score, qs.correct_answers, qs.total_questions,
               qs.results_json, qs.submitted_at, u.avatar
        FROM quiz_submissions qs
        JOIN users u ON qs.student_id = u.id
        WHERE qs.quiz_id = ?
        ORDER BY qs.score DESC, qs.submitted_at ASC
        """,
        (quiz_id,),
    ).fetchall()
    conn.close()

    submission_list = []
    total_score = 0
    highest_score = 0
    passed_count = 0

    for s in subs:
        sc = s["score"]
        total_score += sc
        if sc > highest_score:
            highest_score = sc
        if sc >= 50:
            passed_count += 1
        
        parsed_res = []
        try:
            parsed_res = json.loads(s["results_json"])
        except Exception:
            parsed_res = []

        submission_list.append({
            "id": s["id"],
            "student_id": s["student_id"],
            "student_name": s["student_name"],
            "student_email": s["student_email"],
            "score": sc,
            "correct_answers": s["correct_answers"],
            "total_questions": s["total_questions"],
            "submitted_at": s["submitted_at"],
            "avatar": s["avatar"] or "",
            "results": parsed_res,
        })

    count = len(submission_list)
    avg_score = round(total_score / count, 1) if count else 0
    pass_rate = round((passed_count / count) * 100) if count else 0

    return jsonify({
        "status": "ok",
        "quiz_id": quiz_id,
        "title": quiz_row["title"],
        "status_code": quiz_row["status"],
        "total_submissions": count,
        "average_score": avg_score,
        "highest_score": highest_score,
        "pass_rate": pass_rate,
        "submissions": submission_list,
    })


@app.route("/quiz/export/results/<int:quiz_id>")
@login_required
def quiz_export_results(quiz_id):
    uid = session["user_id"]
    conn = get_db()

    quiz_row = conn.execute(
        """SELECT q.*, c.class_name FROM quizzes q
           LEFT JOIN classrooms c ON q.classroom_id = c.id
           WHERE q.id = ? AND q.faculty_id = ?""",
        (quiz_id, uid),
    ).fetchone()

    if not quiz_row:
        conn.close()
        flash("Quiz not found.", "danger")
        return redirect(url_for("quiz"))

    subs = conn.execute(
        """SELECT qs.*, u.name as student_name, u.email as student_email
           FROM quiz_submissions qs
           JOIN users u ON qs.student_id = u.id
           WHERE qs.quiz_id = ?
           ORDER BY qs.score DESC, qs.submitted_at ASC""",
        (quiz_id,),
    ).fetchall()
    conn.close()

    lines = []
    lines.append("=" * 60)
    lines.append(f"QUIZ RESULTS REPORT: {quiz_row['title'].upper()}")
    lines.append(f"Subject: {quiz_row['subject'] or 'General'}")
    lines.append(f"Classroom: {quiz_row['class_name'] or 'All Students'}")
    lines.append(f"Generated at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Total Submissions: {len(subs)}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"{'Student Name':<25} | {'Email':<25} | {'Score (%)':<10} | {'Correct':<8} | {'Date & Time'}")
    lines.append("-" * 90)

    for s in subs:
        lines.append(f"{s['student_name']:<25} | {s['student_email']:<25} | {str(s['score']) + '%':<10} | {f'{s['correct_answers']}/{s['total_questions']}':<8} | {s['submitted_at']}")

    content = "\n".join(lines)
    filename = f"quiz_{quiz_id}_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    return _quiz_txt_response(filename, content)


@app.route("/quiz/generate", methods=["POST"])
@login_required
def quiz_generate_api():
    pdf_file = request.files.get("quiz_pdf")
    if not pdf_file or not pdf_file.filename:
        return jsonify({"status": "error", "message": "Please choose a PDF file to generate a quiz."}), 400

    if not pdf_file.filename.lower().endswith(".pdf"):
        return jsonify({"status": "error", "message": "Only PDF files are supported."}), 400

    try:
        extracted_text = _extract_pdf_text(pdf_file)
    except Exception as exc:
        return jsonify({"status": "error", "message": f"Could not read the PDF: {exc}"}), 400

    quiz_questions = _build_quiz_questions(extracted_text)
    source_name = secure_filename(pdf_file.filename)

    if not quiz_questions:
        return jsonify({
            "status": "error",
            "message": "No quiz questions could be generated from that PDF. Try a more text-heavy file.",
        }), 400

    session["quiz_draft"] = {
        "source_name": source_name,
        "quiz_questions": quiz_questions,
    }

    return jsonify({
        "status": "ok",
        "source_name": source_name,
        "quiz_questions": quiz_questions,
        "count": len(quiz_questions),
    })


@app.route("/codequest")
@login_required
def codequest():
    return render_template("codequest.html")


@app.route("/codequest/raw")
@login_required
def codequest_raw():
    return send_from_directory(BASE_DIR, "codequest.html")


@app.route("/quiz/export/<int:attempt_id>")
@login_required
def quiz_export_attempt(attempt_id):
    conn = get_db()
    attempt = conn.execute(
        """
        SELECT id, user_id, source_name, total_questions, correct_answers, score,
               quiz_data, quiz_results, created_at
        FROM quiz_attempts
        WHERE id = ? AND user_id = ?
        """,
        (attempt_id, session["user_id"]),
    ).fetchone()
    conn.close()

    if not attempt:
        flash("Quiz attempt not found.", "danger")
        return redirect(url_for("quiz"))

    quiz_questions = json.loads(attempt["quiz_data"])
    quiz_results = json.loads(attempt["quiz_results"])
    txt_content = _build_quiz_txt_content(
        title="AI Study Planner Quiz Attempt",
        quiz_questions=quiz_questions,
        source_name=attempt["source_name"],
        attempt=attempt,
        quiz_results=quiz_results,
    )
    filename = f"quiz_attempt_{attempt_id}.txt"
    return _quiz_txt_response(filename, txt_content)


@app.route("/api/reminders")
@login_required
def api_reminders():
    uid       = session["user_id"]
    today     = date.today()
    today_str = today.strftime("%Y-%m-%d")
    conn      = get_db()

    tasks_due = conn.execute(
        """SELECT task_name, subject, deadline FROM tasks
           WHERE user_id=? AND status='Pending'
           AND deadline >= ? AND deadline <= date(?, '+3 days')
           ORDER BY deadline""",
        (uid, today_str, today_str),
    ).fetchall()

    sessions_today = conn.execute(
        "SELECT subject, study_hours FROM schedules WHERE user_id=? AND date=? AND completed=0",
        (uid, today_str),
    ).fetchall()

    conn.close()

    msgs = []
    for t in tasks_due:
        days_left = (datetime.strptime(t["deadline"], "%Y-%m-%d").date() - today).days
        if days_left == 0:
            msgs.append(f"⚠️ Due TODAY: {t['task_name']} ({t['subject']})")
        elif days_left == 1:
            msgs.append(f"🔴 Due tomorrow: {t['task_name']} ({t['subject']})")
        else:
            msgs.append(f"📅 Due in {days_left} days: {t['task_name']} ({t['subject']})")

    for s in sessions_today:
        msgs.append(f"📚 Study today: {s['subject']} — {s['study_hours']}h scheduled")

    return jsonify({"reminders": msgs})


@app.route("/api/notifications")
@login_required
def api_notifications():
    uid = session["user_id"]
    user_type = session.get("user_type", "student")
    conn = get_db()
    notifications = []

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    try:
        if user_type == "faculty":
            # 1. Student task submissions in faculty's classrooms
            recent_subs = conn.execute(
                """SELECT sub.id, sub.submitted_at, sub.file_name, u.name as student_name,
                          ca.task_name, c.class_name, c.id as class_id
                   FROM classroom_submissions sub
                   JOIN users u ON sub.student_id = u.id
                   JOIN classroom_assignments ca ON sub.assignment_id = ca.id
                   JOIN classrooms c ON sub.classroom_id = c.id
                   WHERE c.faculty_id = ?
                   ORDER BY sub.id DESC LIMIT 6""",
                (uid,),
            ).fetchall()
            for s in recent_subs:
                notifications.append({
                    "id": f"sub-{s['id']}",
                    "type": "submission",
                    "icon": "fas fa-file-circle-check text-success",
                    "title": f"Task Submitted • {s['class_name']}",
                    "text": f"{s['student_name']} submitted work for \"{s['task_name']}\"",
                    "time": s["submitted_at"] if s["submitted_at"] else "Recently",
                    "url": url_for("classrooms", class_id=s["class_id"], tab="tab-tasks")
                })

            # 2. Student enrollments in faculty's classrooms
            recent_joins = conn.execute(
                """SELECT cm.id as member_id, u.name as student_name, c.class_name, c.id as class_id, cm.joined_at
                   FROM classroom_members cm
                   JOIN users u ON cm.student_id = u.id
                   JOIN classrooms c ON cm.classroom_id = c.id
                   WHERE c.faculty_id = ?
                   ORDER BY cm.id DESC LIMIT 5""",
                (uid,),
            ).fetchall()
            for j in recent_joins:
                notifications.append({
                    "id": f"join-{j['member_id']}",
                    "type": "enrollment",
                    "icon": "fas fa-user-plus text-success",
                    "title": f"New Student Joined • {j['class_name']}",
                    "text": f"{j['student_name']} joined {j['class_name']}",
                    "time": j["joined_at"] if j["joined_at"] else "Recently",
                    "url": url_for("classrooms", class_id=j["class_id"], tab="tab-students")
                })

            # 3. Messages from students in faculty's classrooms
            recent_msgs = conn.execute(
                """SELECT m.id, m.message, m.created_at, u.name as sender_name, c.class_name, c.id as class_id
                   FROM classroom_messages m
                   JOIN users u ON m.sender_id = u.id
                   JOIN classrooms c ON m.classroom_id = c.id
                   WHERE c.faculty_id = ? AND m.sender_id != ?
                   ORDER BY m.id DESC LIMIT 6""",
                (uid, uid),
            ).fetchall()
            for m in recent_msgs:
                notifications.append({
                    "id": f"msg-{m['id']}",
                    "type": "message",
                    "icon": "fas fa-comment-dots text-primary",
                    "title": f"Message from {m['sender_name']} • {m['class_name']}",
                    "text": m["message"][:80] + ("..." if len(m["message"]) > 80 else ""),
                    "time": m["created_at"],
                    "url": url_for("classrooms", class_id=m["class_id"], tab="tab-comms")
                })

            # 4. Student quiz submissions in faculty's quizzes
            recent_quiz_subs = conn.execute(
                """SELECT qs.id, qs.quiz_id, qs.student_name, qs.score, qs.correct_answers, qs.total_questions,
                          qs.submitted_at, q.title as quiz_title, c.class_name
                   FROM quiz_submissions qs
                   JOIN quizzes q ON qs.quiz_id = q.id
                   LEFT JOIN classrooms c ON q.classroom_id = c.id
                   WHERE q.faculty_id = ?
                   ORDER BY qs.id DESC LIMIT 6""",
                (uid,),
            ).fetchall()
            for qsub in recent_quiz_subs:
                target_class = f" • {qsub['class_name']}" if qsub['class_name'] else ""
                notifications.append({
                    "id": f"quizsub-{qsub['id']}",
                    "type": "quiz_result",
                    "icon": "fas fa-square-poll-vertical text-purple",
                    "title": f"Quiz Completed: {qsub['quiz_title']}{target_class}",
                    "text": f"{qsub['student_name']} scored {qsub['score']}% ({qsub['correct_answers']}/{qsub['total_questions']})",
                    "time": qsub["submitted_at"] if qsub["submitted_at"] else "Recently",
                    "url": url_for("quiz") + f"?view_quiz={qsub['quiz_id']}"
                })

        else:
            # Student notifications:
            # 0. Active live quizzes assigned by faculty
            active_quizzes = conn.execute(
                """SELECT q.id, q.title, q.subject, q.duration_minutes, q.started_at, q.created_at,
                          c.class_name, u.name as faculty_name,
                          (SELECT COUNT(*) FROM json_each(q.quiz_data)) as question_count
                   FROM quizzes q
                   JOIN users u ON q.faculty_id = u.id
                   LEFT JOIN classrooms c ON q.classroom_id = c.id
                   WHERE q.status = 'active'
                     AND (
                         q.classroom_id IS NULL OR q.classroom_id IN (
                             SELECT classroom_id FROM classroom_members WHERE student_id = ?
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM quiz_submissions qs WHERE qs.quiz_id = q.id AND qs.student_id = ?
                     )
                   ORDER BY q.id DESC LIMIT 4""",
                (uid, uid),
            ).fetchall()
            for aq in active_quizzes:
                target_title = f"Live Quiz: {aq['title']}"
                dur_txt = f"{aq['duration_minutes']}m • " if aq['duration_minutes'] else ""
                class_txt = f" • {aq['class_name']}" if aq['class_name'] else ""
                notifications.append({
                    "id": f"livequiz-{aq['id']}",
                    "type": "quiz",
                    "icon": "fas fa-bolt text-warning",
                    "title": f"{target_title}{class_txt}",
                    "text": f"Professor {aq['faculty_name']} started a live quiz ({dur_txt}{aq['question_count']} questions). Click to attempt now!",
                    "time": aq["started_at"] if aq["started_at"] else (aq["created_at"] or "Now Active"),
                    "url": url_for("quiz_take", quiz_id=aq["id"])
                })

            # 1. Faculty messages & announcements in joined classrooms
            announcements = conn.execute(
                """SELECT m.id, m.message, m.created_at, u.name as faculty_name, c.class_name, c.id as class_id
                   FROM classroom_messages m
                   JOIN users u ON m.sender_id = u.id
                   JOIN classrooms c ON m.classroom_id = c.id
                   JOIN classroom_members cm ON cm.classroom_id = c.id
                   WHERE cm.student_id = ? AND m.sender_id != ?
                   ORDER BY m.id DESC LIMIT 6""",
                (uid, uid),
            ).fetchall()
            for a in announcements:
                notifications.append({
                    "id": f"ann-{a['id']}",
                    "type": "announcement",
                    "icon": "fas fa-bullhorn text-warning",
                    "title": f"Message from {a['faculty_name']} • {a['class_name']}",
                    "text": a["message"][:80] + ("..." if len(a["message"]) > 80 else ""),
                    "time": a["created_at"],
                    "url": url_for("classrooms", class_id=a["class_id"], tab="tab-student-comms")
                })

            # 2. Tasks / assignments assigned to student by faculty
            recent_tasks = conn.execute(
                """SELECT ca.id, ca.task_name, ca.subject, ca.deadline, ca.created_at,
                          c.class_name, c.id as class_id, u.name as faculty_name
                   FROM classroom_assignments ca
                   JOIN classrooms c ON ca.classroom_id = c.id
                   JOIN users u ON c.faculty_id = u.id
                   WHERE ca.student_id = ?
                   ORDER BY ca.id DESC LIMIT 6""",
                (uid,),
            ).fetchall()
            for t in recent_tasks:
                notifications.append({
                    "id": f"cassign-{t['id']}",
                    "type": "assignment",
                    "icon": "fas fa-clipboard-list text-primary",
                    "title": f"New Task Assigned • {t['class_name']}",
                    "text": f"\"{t['task_name']}\" ({t['subject']}) assigned by {t['faculty_name']}. Due: {t['deadline']}",
                    "time": t["created_at"] if t["created_at"] else "Recently",
                    "url": url_for("classrooms", class_id=t["class_id"], tab="tab-student-tasks")
                })

            # 3. New study materials and notes posted in classrooms
            recent_res = conn.execute(
                """SELECT r.id, r.title, r.resource_type, r.created_at, c.class_name, c.id as class_id, u.name as uploader_name
                   FROM classroom_resources r
                   JOIN classrooms c ON r.classroom_id = c.id
                   JOIN classroom_members cm ON cm.classroom_id = c.id
                   JOIN users u ON u.id = r.uploader_id
                   WHERE cm.student_id = ?
                   ORDER BY r.id DESC LIMIT 6""",
                (uid,),
            ).fetchall()
            for res_item in recent_res:
                notifications.append({
                    "id": f"res-{res_item['id']}",
                    "type": "resource",
                    "icon": "fas fa-folder-open text-info",
                    "title": f"New Study Material • {res_item['class_name']}",
                    "text": f"{res_item['title']} ({res_item['resource_type'].upper()}) shared by {res_item['uploader_name']}",
                    "time": res_item["created_at"],
                    "url": url_for("classrooms", class_id=res_item["class_id"], tab="tab-student-materials")
                })

            # 4. Graded & evaluated tasks from faculty
            recent_graded = conn.execute(
                """SELECT sub.id, sub.grade, sub.feedback, sub.graded_at, ca.task_name, c.class_name, c.id as class_id, u.name as faculty_name
                   FROM classroom_submissions sub
                   JOIN classroom_assignments ca ON sub.assignment_id = ca.id
                   JOIN classrooms c ON sub.classroom_id = c.id
                   JOIN users u ON c.faculty_id = u.id
                   WHERE sub.student_id = ? AND (sub.grade IS NOT NULL OR sub.feedback IS NOT NULL)
                   ORDER BY sub.id DESC LIMIT 4""",
                (uid,),
            ).fetchall()
            for g in recent_graded:
                grade_txt = f"Grade: {g['grade']}" if g['grade'] else "Reviewed"
                notifications.append({
                    "id": f"grade-{g['id']}",
                    "type": "grade",
                    "icon": "fas fa-award text-warning",
                    "title": f"Assignment Graded • {g['class_name']}",
                    "text": f"\"{g['task_name']}\" evaluated by {g['faculty_name']}. {grade_txt}",
                    "time": g["graded_at"] if g["graded_at"] else "Recently",
                    "url": url_for("classrooms", class_id=g["class_id"], tab="tab-student-tasks")
                })

            # 5. Upcoming tasks due soon from personal task tracker
            tasks_due = conn.execute(
                """SELECT id, task_name, subject, deadline FROM tasks
                   WHERE user_id=? AND status='Pending'
                   AND deadline >= ? AND deadline <= date(?, '+3 days')
                   ORDER BY deadline LIMIT 3""",
                (uid, today_str, today_str),
            ).fetchall()
            for t in tasks_due:
                notifications.append({
                    "id": f"task-{t['id']}",
                    "type": "task",
                    "icon": "fas fa-clock text-danger",
                    "title": f"Task Due: {t['task_name']}",
                    "text": f"{t['subject']} • Deadline: {t['deadline']}",
                    "time": "Due Soon",
                    "url": url_for("tasks")
                })
    except Exception as e:
        pass

    # Read persistent seen notification IDs from database so seen messages do not return after login
    db_read_ids = set()
    try:
        rows = conn.execute("SELECT notif_id FROM notification_reads WHERE user_id=?", (uid,)).fetchall()
        db_read_ids = {r["notif_id"] for r in rows}
    except Exception:
        pass
    finally:
        conn.close()

    session_read_ids = set(session.get("read_notifications", []))
    all_read_ids = db_read_ids | session_read_ids

    # Filter out all notifications that were already seen / read by this user
    unread_notifications = [n for n in notifications if n["id"] not in all_read_ids]

    return jsonify({
        "count": len(unread_notifications),
        "notifications": unread_notifications[:10]
    })


@app.route("/api/notifications/read", methods=["POST"])
@login_required
def api_notifications_mark_read():
    uid = session["user_id"]
    data = request.get_json(silent=True) or {}
    ids_to_mark = []

    if data.get("all"):
        ids_to_mark = data.get("ids", [])
    elif data.get("id"):
        ids_to_mark = [data.get("id")]

    conn = get_db()
    try:
        for item_id in ids_to_mark:
            item_id_str = str(item_id).strip()
            if not item_id_str:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO notification_reads (user_id, notif_id) VALUES (?, ?)",
                (uid, item_id_str),
            )
            # If notification is a classroom message, mark read in classroom_messages table
            if item_id_str.startswith("msg-") or item_id_str.startswith("dm-") or item_id_str.startswith("ann-"):
                try:
                    raw_msg_id = int(item_id_str.split("-", 1)[1])
                    if session.get("user_type") == "faculty":
                        conn.execute("UPDATE classroom_messages SET read_by_faculty=1 WHERE id=?", (raw_msg_id,))
                    else:
                        conn.execute("UPDATE classroom_messages SET read_by_student=1 WHERE id=?", (raw_msg_id,))
                except Exception:
                    pass
        conn.commit()
    except Exception:
        pass

    db_read_rows = []
    try:
        db_read_rows = conn.execute("SELECT notif_id FROM notification_reads WHERE user_id=?", (uid,)).fetchall()
    except Exception:
        pass
    finally:
        conn.close()

    all_reads = [row["notif_id"] for row in db_read_rows]
    session["read_notifications"] = all_reads
    return jsonify({"success": True, "read_count": len(all_reads)})




# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 5000))
    debug_mode = not _is_hosted_runtime() and os.environ.get("FLASK_DEBUG", "1") != "0"
    local_url = f"http://127.0.0.1:{port}"

    # Attempt to determine a LAN-accessible IP address for network URL
    network_url = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            network_url = f"http://{ip}:{port}"
    except Exception:
        network_url = None

    # Build a neat dynamic box based on content width
    lines = ["AI Smart Collaborative Learning Platform", "", f"Local:   {local_url}"]
    if network_url:
        lines.append(f"Network: {network_url}")
    lines.append("")
    lines.append("Press Ctrl+C to stop")

    inner_width = max(len(l) for l in lines) + 2
    print("+" + "-" * inner_width + "+")
    for l in lines:
        print("| " + l.ljust(inner_width - 1) + "|")
    print("+" + "-" * inner_width + "+")

    # Open the browser once when the reloader child process runs (avoids double-open)
    if not _is_hosted_runtime() and os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        try:
            webbrowser.open_new_tab(local_url)
        except Exception:
            pass

    app.run(debug=debug_mode, host=host, port=port)
