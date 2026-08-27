import json
import os
import re
import urllib.parse
import httpx

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


async def get_youtube_video_id(query: str) -> str:
    """Searches YouTube and returns a real, playable video ID."""
    if not query:
        return ""
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
            matches = re.findall(r'"videoId":"([a-zA-Z0-9_-]{11})"', resp.text)
            if matches:
                # Return the first found video ID
                return matches[0]
    except Exception as e:
        print(f"Error fetching YouTube ID for '{query}': {e}")
    return ""


_BLOG_BLOCKED_DOMAINS = (
    "youtube.com", "youtu.be", "duckduckgo.com", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "pinterest.com", "tiktok.com",
    "reddit.com", "bing.com", "google.com",
)

# Some sites/browsers reject requests that don't look like a real browser.
# Rotating between a couple of common, realistic User-Agents (and retrying
# once on failure) makes the scrapers noticeably less flaky.
_SCRAPE_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15",
)

BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY")


def _resolve_ddg_redirect(href: str) -> str:
    """DuckDuckGo's HTML results wrap outbound links in a redirect URL
    like //duckduckgo.com/l/?uddg=<encoded-target>&rut=... — unwrap it."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    qs = urllib.parse.parse_qs(parsed.query)
    if "uddg" in qs and qs["uddg"]:
        return urllib.parse.unquote(qs["uddg"][0])
    return href


def _looks_like_article(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return bool(host) and not any(blocked in host for blocked in _BLOG_BLOCKED_DOMAINS)


async def _get_with_retry(client: httpx.AsyncClient, url: str) -> httpx.Response | None:
    """GETs a URL, retrying once with a different User-Agent if the first
    attempt fails or gets blocked (non-200)."""
    last_resp = None
    for ua in _SCRAPE_USER_AGENTS:
        try:
            resp = await client.get(url, headers={"User-Agent": ua})
            if resp.status_code == 200:
                return resp
            last_resp = resp
        except Exception:
            continue
    return last_resp


async def _search_duckduckgo(client: httpx.AsyncClient, query: str) -> str:
    """Layer 1: scrape DuckDuckGo's no-JS HTML results page."""
    url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
    resp = await _get_with_retry(client, url)
    if not resp or resp.status_code != 200:
        return ""
    raw_hrefs = re.findall(r'class="result__a"[^>]*href="([^"]+)"', resp.text)
    for raw_href in raw_hrefs:
        link = _resolve_ddg_redirect(raw_href)
        if link and _looks_like_article(link):
            return link
    return ""


async def _search_bing(client: httpx.AsyncClient, query: str) -> str:
    """Layer 2: scrape Bing's HTML results page as a fallback source, in
    case DuckDuckGo is unreachable, rate-limited, or changes its markup."""
    url = f"https://www.bing.com/search?q={urllib.parse.quote(query)}&count=10"
    resp = await _get_with_retry(client, url)
    if not resp or resp.status_code != 200:
        return ""
    # Bing marks organic results with <li class="b_algo">...<a href="...">
    raw_hrefs = re.findall(r'<li class="b_algo"[^>]*>.*?<a href="([^"]+)"', resp.text, re.DOTALL)
    for raw_href in raw_hrefs:
        if raw_href and _looks_like_article(raw_href):
            return raw_href
    return ""


async def _search_brave_api(client: httpx.AsyncClient, query: str) -> str:
    """Layer 3: a real search API (Brave Search) used as the final,
    most-reliable fallback if BRAVE_API_KEY is configured. Brave offers a
    free tier (see https://brave.com/search/api/) - set BRAVE_API_KEY as an
    environment variable to enable this layer."""
    if not BRAVE_API_KEY:
        return ""
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": BRAVE_API_KEY,
    }
    try:
        resp = await client.get(url, headers=headers, params={"q": query, "count": 10})
    except Exception as e:
        print(f"Error querying Brave Search API for '{query}': {e}")
        return ""
    if resp.status_code != 200:
        return ""
    try:
        results = resp.json().get("web", {}).get("results", [])
    except Exception:
        return ""
    for item in results:
        link = item.get("url", "")
        if link and _looks_like_article(link):
            return link
    return ""


async def get_blog_link(query: str) -> str:
    """Finds a real, relevant article/blog URL for a subtopic so the user
    is taken straight to a specific page instead of a search results page.

    Tries three layers in order, falling through only if one fails:
      1. DuckDuckGo HTML scrape (no key required)
      2. Bing HTML scrape (no key required, different engine as backup)
      3. Brave Search API (requires BRAVE_API_KEY env var, most reliable)
    """
    if not query:
        return ""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for search_fn in (_search_duckduckgo, _search_bing, _search_brave_api):
            try:
                link = await search_fn(client, query)
                if link:
                    return link
            except Exception as e:
                print(f"Error in {search_fn.__name__} for '{query}': {e}")
    return ""


def _clean_json(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and len(data) == 1 and isinstance(list(data.values())[0], dict):
            inner = list(data.values())[0]
            if "modules" in inner or "title" in inner:
                return inner
        return data
    except Exception as e:
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
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
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
        "- Video query: specific YouTube search phrase (8 words or fewer).\n"
        "- Blog query: a specific search phrase to find one good written article "
        "or blog post on this subtopic (8 words or fewer)."
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
                        "blogQuery": {"type": "STRING"},
                    },
                    "required": ["title", "notes", "videoQuery", "blogQuery"],
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
        # Finds and attaches the real YouTube video ID:
        m["videoId"] = await get_youtube_video_id(m.get("videoQuery", topic))
        # Finds and attaches a real, relevant blog/article URL:
        m["blogUrl"] = await get_blog_link(m.get("blogQuery") or m.get("videoQuery", topic))

    return parsed


async def generate_module(course_title: str, lesson_topic: str) -> dict:
    prompt = (
        f'Course: "{course_title}". Write the lesson module for: "{lesson_topic}".\n'
        "Also include a blog query: a specific search phrase to find one good "
        "written article or blog post on this subtopic (8 words or fewer)."
    )
    module_schema = {
        "type": "OBJECT",
        "properties": {
            "title": {"type": "STRING"},
            "notes": {"type": "STRING"},
            "videoQuery": {"type": "STRING"},
            "blogQuery": {"type": "STRING"},
        },
        "required": ["title", "notes", "videoQuery", "blogQuery"],
    }

    raw = await _call_gemini(prompt, schema=module_schema, max_tokens=2000)
    parsed = _clean_json(raw)

    if not isinstance(parsed, dict) or not parsed.get("title") or not parsed.get("notes"):
        raise AIError("Incomplete lesson data returned by the model.")

    parsed["completed"] = False
    parsed["quiz"] = None
    parsed["videoId"] = await get_youtube_video_id(parsed.get("videoQuery", lesson_topic))
    parsed["blogUrl"] = await get_blog_link(parsed.get("blogQuery") or parsed.get("videoQuery", lesson_topic))
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
