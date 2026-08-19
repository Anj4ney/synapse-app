import json
import os
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
    raw = raw.strip()
    if raw.startswith("```"):
        parts = raw.split("\n", 1)
        raw = parts[1] if len(parts) > 1 else ""
        if raw.endswith("```"):
            raw = raw[:-3]
    raw = raw.strip()
    if raw[:4].lower() == "json":
        raw = raw[4:].strip()
    return json.loads(raw)


async def _call_gemini(system_prompt: str, user_message: str, max_tokens: int = 1200) -> str:
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
        '{"title":"string","description":"string, 20 words or fewer",'
        '"modules":[{"title":"string","notes":"markdown string, 60-90 words, may use a short heading '
        'and a short bullet list","videoQuery":"a good, specific YouTube search phrase, 8 words or fewer"}]}\n'
        "Include exactly 4 modules, ordered from foundational to advanced."
    )
    raw = await _call_gemini(system_prompt, f"Design a short course on: {topic}", max_tokens=1500)
    try:
        parsed = _clean_json(raw)
    except Exception:
        raise AIError("Unexpected response format from the model.")

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
        '{"title":"string","notes":"markdown string, 60-90 words, may use a short heading and a short '
        'bullet list","videoQuery":"a specific YouTube search phrase, 8 words or fewer"}'
    )
    raw = await _call_gemini(
        system_prompt,
        f'Course: "{course_title}". Write the lesson: "{lesson_topic}".',
        max_tokens=800,
    )
    try:
        parsed = _clean_json(raw)
    except Exception:
        raise AIError("Unexpected response format from the model.")

    if not parsed.get("title") or not parsed.get("notes"):
        raise AIError("Incomplete lesson data returned by the model.")

    parsed["completed"] = False
    parsed["quiz"] = None
    return parsed


async def generate_quiz(module_title: str, module_notes: str) -> dict:
    system_prompt = (
        "Write a short quiz testing the lesson below. Respond with ONLY compact JSON matching this shape:\n"
        '{"questions":[{"prompt":"string","options":[{"id":"a","text":"string"},{"id":"b","text":"string"},'
        '{"id":"c","text":"string"}],"correctId":"string","explanation":"string, under 25 words"}]}\n'
        "Write exactly 3 questions, each with exactly 3 options and one correct option id."
    )
    raw = await _call_gemini(
        system_prompt,
        f'Lesson title: "{module_title}"\nLesson notes:\n{module_notes}',
        max_tokens=1000,
    )
    try:
        parsed = _clean_json(raw)
    except Exception:
        raise AIError("Unexpected response format from the model.")

    if not isinstance(parsed.get("questions"), list) or not parsed["questions"]:
        raise AIError("Incomplete quiz data returned by the model.")
    return parsed
