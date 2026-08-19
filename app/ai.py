import json
import os
import re
import httpx

# In Vercel, set GEMINI_API_KEY in Settings > Environment Variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class AIError(Exception):
    pass


def _require_key():
    if not GEMINI_API_KEY:
        raise AIError(
            "The server is missing GEMINI_API_KEY. Get a key from Google AI Studio "
            "(aistudio.google.com) and set it as an environment variable in Vercel."
        )


def _clean_json(raw: str) -> dict:
    """Robust JSON cleaner that handles codeblocks, extra text, and raw JSON."""
    raw = raw.strip()
    
    # 1. Direct parse attempt
    try:
        return json.loads(raw)
    except Exception:
        pass

    # 2. Extract code block if enclosed in ```
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except Exception:
                pass

    # 3. Extract the first { ... } JSON object found in text
    match = re.search(r"(\{[\s\S]*\})", raw)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    raise AIError("Unexpected response format from the model.")


async def _call_gemini(system_prompt: str, user_message: str, max_tokens: int = 4000) -> str:
    _require_key()
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "content-type": "application/json",
    }
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_message}],
            }
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}],
        },
        "generationConfig": {
            "responseMimeType": "application/json",
            "maxOutputTokens": max_tokens,
            "temperature": 0.7,
        },
    }
    
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise AIError(f"Could not reach the Gemini API: {e}")

    if resp.status_code != 200:
        raise AIError(f"Gemini API error ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    candidates = data.get("candidates", [])
    if not candidates:
        raise AIError("No response candidates returned by Gemini.")

    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if "text" in part)
    if not text:
        raise AIError("No text returned from Gemini.")
    return text


async def generate_course(topic: str) -> dict:
    system_prompt = (
        "You are a curriculum designer. Respond with ONLY compact JSON matching exactly this shape:\n"
        '{\n'
        '  "title": "Course Title",\n'
        '  "description": "Short description (20 words or fewer)",\n'
        '  "modules": [\n'
        '    {\n'
        '      "title": "Module Title",\n'
        '      "notes": "Short markdown lesson notes (60-90 words)",\n'
        '      "videoQuery": "YouTube search phrase (8 words or fewer)"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "Include exactly 4 modules, ordered from foundational to advanced."
    )
    raw = await _call_gemini(
        system_prompt, 
        f"Design a short course on: {topic}", 
        max_tokens=4000
    )
    
    parsed = _clean_json(raw)

    if not parsed.get("title") or not isinstance(parsed.get("modules"), list) or not parsed["modules"]:
        raise AIError("Incomplete course data returned by the model.")

    for m in parsed["modules"]:
        m["completed"] = False
        m["quiz"] = None
    return parsed


async def generate_module(course_title: str, lesson_topic: str) -> dict:
    system_prompt = (
        "You are a curriculum designer writing one lesson for an existing course. "
        "Respond with ONLY compact JSON matching this shape:\n"
        '{\n'
        '  "title": "Lesson Title",\n'
        '  "notes": "Markdown lesson notes (60-90 words)",\n'
        '  "videoQuery": "Specific YouTube search phrase (8 words or fewer)"\n'
        '}'
    )
    raw = await _call_gemini(
        system_prompt,
        f'Course: "{course_title}". Write the lesson: "{lesson_topic}".',
        max_tokens=2000,
    )
    
    parsed = _clean_json(raw)

    if not parsed.get("title") or not parsed.get("notes"):
        raise AIError("Incomplete lesson data returned by the model.")

    parsed["completed"] = False
    parsed["quiz"] = None
    return parsed


async def generate_quiz(module_title: str, module_notes: str) -> dict:
    system_prompt = (
        "Write a short quiz testing the lesson below. Respond with ONLY compact JSON matching this shape:\n"
        '{\n'
        '  "questions": [\n'
        '    {\n'
        '      "prompt": "Question text",\n'
        '      "options": [\n'
        '        {"id": "a", "text": "Option A"},\n'
        '        {"id": "b", "text": "Option B"},\n'
        '        {"id": "c", "text": "Option C"}\n'
        '      ],\n'
        '      "correctId": "a",\n'
        '      "explanation": "Explanation under 25 words"\n'
        '    }\n'
        '  ]\n'
        '}\n'
        "Write exactly 3 questions, each with exactly 3 options (id a, b, c) and one correct option id."
    )
    raw = await _call_gemini(
        system_prompt,
        f'Lesson title: "{module_title}"\nLesson notes:\n{module_notes}',
        max_tokens=2500,
    )
    
    parsed = _clean_json(raw)

    if not isinstance(parsed.get("questions"), list) or not parsed["questions"]:
        raise AIError("Incomplete quiz data returned by the model.")
    return parsed
