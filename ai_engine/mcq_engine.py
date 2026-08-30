"""
AI Engine — MCQ & Quiz Generator
================================
Provides intelligent parsing and extraction of MCQ test sheets from PDF/Text,
answer key detection and mapping, AI question synthesis, and fallback NLP generation.
"""

import re
import os
import json
from typing import List, Dict, Any, Optional


def _clean_text_line(line: str) -> str:
    """Normalize line whitespace."""
    return re.sub(r"[ \t]+", " ", line or "").strip()


def parse_answer_key_section(text: str) -> Dict[int, str]:
    """
    Search for and parse an answer key section at the end of or within the text.
    Returns a dict mapping question number (1-based integer) to option letter (e.g. 'A', 'B', 'C', 'D').
    """
    answers = {}

    # Look for Answer Key headers
    key_header_pattern = re.compile(
        r"(?:^|\n)\s*(?:[-=*#_\s]*)\s*(?:Answer\s*Key|Answers|Solutions?|Correct\s*Answers?|Key\s*Sheet|Keys?)\s*[:\-–—]?\s*(?:[-=*#_\s]*)\s*\n",
        re.IGNORECASE
    )
    
    headers = list(key_header_pattern.finditer(text))
    if headers:
        # Take the last answer key header section
        last_header = headers[-1]
        key_section = text[last_header.end():]
    else:
        # If no explicit header, check if the end of text looks like an answer key list
        lines = text.strip().split("\n")
        tail_lines = lines[-25:] if len(lines) > 25 else lines
        key_section = "\n".join(tail_lines)

    # Patterns for answers:
    # "1. B", "1 - B", "1: B", "1) B", "Q1: B", "1. (B)", "1. (b)", "1. B.", "(1) B"
    item_pattern = re.compile(
        r"(?:^|[,\s;\n|])(?:Q(?:uestion)?\s*[\.:]?)?\s*\(?(\d+)\)?\s*[\.\-–—:)]\s*\(?([A-Ea-e])\)?(?:\b|[.\s,]|$)"
    )

    for match in item_pattern.finditer(key_section):
        q_num = int(match.group(1))
        ans_letter = match.group(2).upper()
        if q_num not in answers:
            answers[q_num] = ans_letter

    return answers


def extract_inline_answer(text_block: str) -> Optional[str]:
    """
    Extract inline answer if embedded in question text, e.g.
    'Ans: B', 'Answer: (C)', 'Correct Answer - A'
    """
    inline_pattern = re.compile(
        r"(?:Ans(?:wer)?|Correct(?:\s*Option)?|Key)\s*[:\-–—]\s*\(?([A-Ea-e])\)?(?:\.|\b|$)",
        re.IGNORECASE
    )
    match = inline_pattern.search(text_block)
    if match:
        return match.group(1).upper()
    return None


def parse_mcq_test(text: str) -> List[Dict[str, Any]]:
    """
    Intelligently parse questions and options from an MCQ test sheet document text.
    Handles multi-line questions, options, split options across pages, and maps the answer key.
    """
    if not text or not text.strip():
        return []

    # First, separate Answer Key section from Question section if distinct header exists
    key_header_pattern = re.compile(
        r"(?:^|\n)\s*(?:[-=*#_\s]*)\s*(?:Answer\s*Key|Answers|Solutions?|Correct\s*Answers?|Key\s*Sheet)\s*[:\-–—]?\s*(?:[-=*#_\s]*)\s*\n",
        re.IGNORECASE
    )
    
    header_match = key_header_pattern.search(text)
    if header_match:
        questions_text = text[:header_match.start()]
        key_section_text = text[header_match.start():]
    else:
        questions_text = text
        key_section_text = text

    answer_keys = parse_answer_key_section(key_section_text)

    # Normalize line breaks and clean whitespace
    lines = questions_text.split("\n")
    cleaned_lines = []
    
    # Filter out obvious header/footer/page noise
    noise_patterns = [
        re.compile(r"^page\s*\d+\s*(?:of\s*\d+)?$", re.IGNORECASE),
        re.compile(r"^instructions\s*:\s*.*$", re.IGNORECASE),
        re.compile(r"^choose the best answer.*$", re.IGNORECASE),
        re.compile(r"^try the test without looking.*$", re.IGNORECASE),
    ]

    for raw_line in lines:
        line = _clean_text_line(raw_line)
        if not line:
            continue
        if any(np.match(line) for np in noise_patterns):
            continue
        cleaned_lines.append(line)

    normalized_content = "\n".join(cleaned_lines)

    # Regex to split on question starts:
    # e.g., "1.", "2.", "Q1.", "Question 1:", "1)", "(1)"
    q_start_regex = re.compile(
        r"(?:^|\n)\s*(?:Q(?:uestion)?\s*[\.:]?)?\s*\(?(\d{1,3})\)?\s*[\.\-–—:)]\s+",
        re.IGNORECASE
    )

    q_matches = list(q_start_regex.finditer(normalized_content))
    if not q_matches:
        return []

    extracted_questions = []

    for i, match in enumerate(q_matches):
        q_num = int(match.group(1))
        start_pos = match.end()
        end_pos = q_matches[i + 1].start() if (i + 1 < len(q_matches)) else len(normalized_content)

        block = normalized_content[start_pos:end_pos].strip()

        # Check for inline answer before stripping options
        inline_ans = extract_inline_answer(block)

        # Parse Options: A. ..., B. ..., C. ..., D. ... or (A), (B)...
        opt_regex = re.compile(
            r"(?:^|\n|\s{2,})\s*\(?([A-Ea-e])\)?\s*[\.\-–—:)]\s+",
            re.IGNORECASE
        )

        opt_matches = list(opt_regex.finditer(block))

        if not opt_matches or len(opt_matches) < 2:
            # Maybe options are on a single line like "A. ... B. ... C. ... D. ..."
            inline_opt_regex = re.compile(
                r"\s+\(?([A-Ea-e])\)?\s*[\.\-–—:)]\s+"
            )
            opt_matches = list(inline_opt_regex.finditer(block))

        if not opt_matches or len(opt_matches) < 2:
            continue

        question_stem = block[:opt_matches[0].start()].strip()
        question_stem = re.sub(r"\s*\n\s*", " ", question_stem)

        options = []
        opt_dict = {}

        for j, o_match in enumerate(opt_matches):
            letter = o_match.group(1).upper()
            o_start = o_match.end()
            o_end = opt_matches[j + 1].start() if (j + 1 < len(opt_matches)) else len(block)
            opt_val = block[o_start:o_end].strip()

            # Clean inline answer noise from option value if present
            opt_val = re.sub(r"(?:Ans(?:wer)?|Correct|Key)\s*[:\-–—].*$", "", opt_val, flags=re.IGNORECASE).strip()
            opt_val = re.sub(r"\s*\n\s*", " ", opt_val)

            if opt_val:
                options.append(opt_val)
                opt_dict[letter] = opt_val

        # Determine the correct answer
        correct_answer = ""
        # 1. Check Answer Key dict by question index/number
        target_letter = answer_keys.get(q_num) or answer_keys.get(len(extracted_questions) + 1)
        if not target_letter and inline_ans:
            target_letter = inline_ans

        if target_letter and target_letter in opt_dict:
            correct_answer = opt_dict[target_letter]
        elif target_letter and len(options) >= (ord(target_letter) - ord('A') + 1):
            idx = ord(target_letter) - ord('A')
            if 0 <= idx < len(options):
                correct_answer = options[idx]

        if not correct_answer and options:
            # Fallback to first option if unknown, so question is valid
            correct_answer = options[0]

        if question_stem and len(options) >= 2:
            extracted_questions.append({
                "question": question_stem,
                "options": options,
                "answer": correct_answer,
                "q_number": q_num
            })

    # Sort questions by q_number
    extracted_questions.sort(key=lambda q: q.get("q_number", 0))

    # Clean up q_number before returning
    for q in extracted_questions:
        q.pop("q_number", None)

    return extracted_questions


def generate_mcq_with_openai(text: str, limit: int = 10) -> Optional[List[Dict[str, Any]]]:
    """
    Attempt to use OpenAI API to parse/generate MCQs if an API key is available.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        prompt = f"""You are an expert AI quiz generator.
Extract or synthesize up to {limit} multiple-choice questions from the following text.
If the text is already an MCQ test, extract all questions, their options, and correct answers accurately (using the answer key if provided).
Return ONLY a valid JSON array of objects with keys:
- "question": string (the question text)
- "options": list of 4 strings (the multiple choices)
- "answer": string (the exact correct answer string matching one of the options)

Text:
\"\"\"{text[:12000]}\"\"\"
"""
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            response_format={"type": "json_object"}
        )

        content = response.choices[0].message.content
        data = json.loads(content)
        if isinstance(data, dict):
            questions = data.get("questions") or data.get("quiz") or list(data.values())[0]
        else:
            questions = data

        if isinstance(questions, list) and questions:
            cleaned = []
            for item in questions:
                if "question" in item and "options" in item and "answer" in item:
                    cleaned.append({
                        "question": str(item["question"]),
                        "options": [str(o) for o in item["options"]],
                        "answer": str(item["answer"])
                    })
            if cleaned:
                return cleaned
    except Exception as e:
        print(f"[MCQ Engine] OpenAI generation skipped: {e}")

    return None


def build_smart_quiz(text: str, fallback_builder_fn, limit: int = 50) -> List[Dict[str, Any]]:
    """
    Master function:
    1. First tries local structured MCQ parsing (detects question numbers, options A/B/C/D, answer keys).
    2. If text contains pre-formatted MCQs, returns them immediately with correct answers.
    3. If not pre-formatted MCQs, attempts OpenAI API if key configured.
    4. Otherwise falls back to NLP heuristic generation.
    """
    if not text or not text.strip():
        return []

    # 1. Check if the PDF has structured MCQ questions
    parsed_mcqs = parse_mcq_test(text)
    if parsed_mcqs and len(parsed_mcqs) >= 2:
        return parsed_mcqs[:limit]

    # 2. Try OpenAI API if available
    llm_mcqs = generate_mcq_with_openai(text, limit=min(limit, 15))
    if llm_mcqs:
        return llm_mcqs

    # 3. Fallback to existing NLP heuristic builder
    return fallback_builder_fn(text, limit=limit)
