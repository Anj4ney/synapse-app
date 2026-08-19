import json
import os
import re
import httpx
import re
import urllib.parse

async def get_youtube_video_id(query: str) -> str:
    """Searches YouTube and returns the first matching video ID."""
    try:
        encoded_query = urllib.parse.quote(query)
        url = f"https://www.youtube.com/results?search_query={encoded_query}"
        
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            
        if resp.status_code == 200:
            # Find the first 11-character video ID in YouTube search results
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if matches:
                # Return the first found valid video ID
                return matches[0]
    except Exception as e:
        print(f"Error fetching YouTube ID for '{query}': {e}")
    
    return ""

# In Vercel, set GEMINI_API_KEY in Settings > Environment Variables
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"


class AIError(Exception):
    pass


def _require_key():
    if not GEMINI_API_KEY:
        raise AIError(
            "The server is missing GEMINI_API_KEY. Set it as an environment variable in Vercel."
        )


def _clean_json(raw: str) -> dict:
    """Robust JSON cleaner."""
    raw = raw.strip()
    
    # Remove markdown code fences if present
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()

    try:
        data = json.loads(raw)
        # If wrapped in a single outer key like {"course": {...}}, unwrap it
        if isinstance(data, dict) and len(data) == 1 and isinstance(list(data.values())[0], dict):
            inner = list(data.values())[0]
            if "modules" in inner or "title" in inner:
                return inner
        return data
    except Exception as e:
        # Fallback: find first outer { ... }
        match = re.search(r"(\{[\s\S]*\})", raw)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except Exception:
                pass
        raise AIError(f"Could not parse JSON response: {e}")


async def _call_gemini(prompt: str, schema: dict = None, max_tokens: int = 4000) -> str:
    _require_key()
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"
    headers = {
        "x-goog-api-key": GEMINI_API_KEY,
        "content-type": "application/json",
    }
    
    gen_config = {
        "responseMimeType": "application/json",
        "maxOutputTokens": max_tokens,
        "temperature": 0.7,
    }
    if schema:
        gen_config["responseSchema"] = schema

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": gen_config,
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
    prompt = (
        f"Design a high-quality 4-module short course on: {topic}.\n"
        "Requirements:\n"
        "- Title: concise course title.\n"
        "- Description: 20 words or fewer.\n"
        "- Exactly 4 modules ordered from foundational to advanced.\n"
        "- Module notes: 60-90 words in markdown.\n"
        "- Video query: specific YouTube search phrase (8 words or fewer)."
    )
    
    course_schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "description": {"type": "STRING"},
            "modules": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "title": {"type": "STRING"},
                        "notes": {"type": "STRING"},
                        "videoQuery": {"type": "STRING"},
                    },
                    "required": ["title", "notes", "videoQuery"],
                },
            },
        },
        "required": ["title", "description", "modules"],
    }

    raw = await _call_gemini(prompt, schema=course_schema, max_tokens=4000)
    parsed = _clean_json(raw)

    if not isinstance(parsed, dict) or not parsed.get("title") or not isinstance(parsed.get("modules"), list):
        raise AIError("Incomplete course data returned by the model.")

    for m in parsed["modules"]:
        m["completed"] = False
        m["quiz"] = None
    return parsed


async def generate_module(course_title: str, lesson_topic: str) -> dict:
    prompt = f'Course: "{course_title}". Write the lesson module for: "{lesson_topic}".'
    
    module_schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "notes": {"type": "STRING"},
            "videoQuery": {"type": "STRING"},
        },
        "required": ["title", "notes", "videoQuery"],
    }

    raw = await _call_gemini(prompt, schema=module_schema, max_tokens=2000)
    parsed = _clean_json(raw)

    if not isinstance(parsed, dict) or not parsed.get("title") or not parsed.get("notes"):
        raise AIError("Incomplete lesson data returned by the model.")

    parsed["completed"] = False
    parsed["quiz"] = None
    return parsed


async def generate_quiz(module_title: str, module_notes: str) -> dict:
    prompt = (
        f'Lesson title: "{module_title}"\n'
        f'Lesson notes:\n{module_notes}\n\n'
        "Write exactly 3 multiple-choice quiz questions testing this lesson with 3 options each (id: a, b, c)."
    )
    
    quiz_schema = {
        "type": "OBJECT",
        "properties": {
            "questions": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "prompt": {"type": "STRING"},
                        "options": {
                            "type": "ARRAY",
                            "items": {
                                "type": "OBJECT",
                                "properties": {
                                    "id": {"type": "STRING"},
                                    "text": {"type": "STRING"},
                                },
                                "required": ["id", "text"],
                            },
                        },
                        "correctId": {"type": "STRING"},
                        "explanation": {"type": "STRING"},
                    },
                    "required": ["prompt", "options", "correctId", "explanation"],
                },
            },
        },
        "required": ["questions"],
    }

    raw = await _call_gemini(prompt, schema=quiz_schema, max_tokens=2500)
    parsed = _clean_json(raw)

    if not isinstance(parsed, dict) or not isinstance(parsed.get("questions"), list) or not parsed["questions"]:
        raise AIError("Incomplete quiz data returned by the model.")
    return parsed
