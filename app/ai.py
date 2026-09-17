import asyncio
import json
import os
import random
import re
import time
import urllib.parse
from dataclasses import dataclass
import httpx


def _load_gemini_keys() -> list[str]:
    """Reads GEMINI_API_KEY_1 / _2 / _3, in order, skipping any that are
    missing so the app still works with only 1 or 2 configured. Falls back
    to the old single GEMINI_API_KEY variable if none of the numbered ones
    are set, so existing deployments don't break on upgrade."""
    keys = [
        v for v in (
            os.environ.get("GEMINI_API_KEY_1"),
            os.environ.get("GEMINI_API_KEY_2"),
            os.environ.get("GEMINI_API_KEY_3"),
        )
        if v
    ]
    if not keys:
        legacy = os.environ.get("GEMINI_API_KEY")
        if legacy:
            keys.append(legacy)
    return keys


GEMINI_API_KEYS = _load_gemini_keys()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# How long (seconds) a project is skipped after a quota/rate-limit error
# before it's tried again. Overridable via env for tuning without a code
# change.
_QUOTA_COOLDOWN_SECONDS = float(os.environ.get("GEMINI_QUOTA_COOLDOWN_SECONDS", "60"))

# Substrings that identify a quota/rate-limit response body, in addition to
# a bare HTTP 429. Checked case-insensitively.
_QUOTA_ERROR_MARKERS = (
    "resource_exhausted",
    "quota_exceeded",
    "quota exceeded",
    "rate limit",
    "rate-limit",
)

# HTTP status codes that mean "temporary service/model overload", as
# distinct from quota exhaustion (429). A project hitting one of these is
# NOT out of quota - it's worth retrying, and only deprioritized (not
# quota-cooled) if it keeps failing.
_TRANSIENT_STATUS_CODES = (500, 503, 504)

# Substrings that identify a transient/overload response body, in addition
# to the status codes above. Checked case-insensitively, and only after
# ruling out a quota error first (see _call_gemini) so the two never overlap.
_TRANSIENT_ERROR_MARKERS = (
    "unavailable",
    "service_unavailable",
    "overloaded",
)

# Exponential backoff schedule (seconds) for retrying the SAME project on a
# transient error before giving up on it and rotating to the next one.
# attempt 1 -> ~1s, attempt 2 -> ~2s, attempt 3 -> ~4s. A small amount of
# random jitter is added on top of each delay to avoid retry storms when
# many concurrent requests hit the same transient outage at once.
_TRANSIENT_MAX_RETRIES = int(os.environ.get("GEMINI_TRANSIENT_MAX_RETRIES", "3"))
_TRANSIENT_BASE_DELAY_SECONDS = float(os.environ.get("GEMINI_TRANSIENT_BASE_DELAY_SECONDS", "1.0"))
_TRANSIENT_JITTER_RATIO = 0.25

# How long (seconds) a project is deprioritized after exhausting its
# transient-error retries, before it's eligible to be tried again. Shorter
# than the quota cooldown since a 503 is usually a passing overload, not a
# multi-minute quota reset.
_TRANSIENT_COOLDOWN_SECONDS = float(os.environ.get("GEMINI_TRANSIENT_COOLDOWN_SECONDS", "20"))


class AIError(Exception):
    pass


@dataclass
class _GeminiProject:
    label: str
    api_key: str
    unavailable_until: float = 0.0  # monotonic time; 0 means available now

    def is_available(self, now: float) -> bool:
        return now >= self.unavailable_until


class _GeminiKeyPool:
    """Tracks quota availability across the configured Gemini projects and
    rotates between them on quota/rate-limit errors only.

    All state (which project is "active", which are cooling down) is kept
    in memory for this process and mutated only while holding `_lock`, so
    concurrent requests can't corrupt it or pile onto a project another
    request just found exhausted.
    """

    def __init__(self, keys: list[str]):
        self._projects = [
            _GeminiProject(label=f"Project {i + 1}", api_key=k)
            for i, k in enumerate(keys)
        ]
        self._active_index = 0
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._projects)

    async def ordered_available_projects(self) -> list[_GeminiProject]:
        """Snapshot of currently-available projects, starting with the
        preferred (active) one and wrapping around the rest in order."""
        async with self._lock:
            now = time.monotonic()
            n = len(self._projects)
            order = [(self._active_index + i) % n for i in range(n)]
            return [self._projects[i] for i in order if self._projects[i].is_available(now)]

    async def mark_quota_exceeded(self, project: "_GeminiProject") -> None:
        async with self._lock:
            project.unavailable_until = time.monotonic() + _QUOTA_COOLDOWN_SECONDS
            # Shift preference to the next project immediately so other
            # concurrent/subsequent requests stop picking the one we just
            # learned is exhausted, instead of each discovering it the hard way.
            idx = self._projects.index(project)
            self._active_index = (idx + 1) % len(self._projects)

    async def mark_transient_unavailable(self, project: "_GeminiProject") -> None:
        """Deprioritize a project after it exhausted its 503/overload retry
        attempts. Shorter cooldown than a quota exhaustion - this is not a
        quota signal, just "give it a little time before trying again"."""
        async with self._lock:
            project.unavailable_until = time.monotonic() + _TRANSIENT_COOLDOWN_SECONDS
            idx = self._projects.index(project)
            self._active_index = (idx + 1) % len(self._projects)

    async def mark_active(self, project: "_GeminiProject") -> None:
        async with self._lock:
            self._active_index = self._projects.index(project)


_gemini_pool = _GeminiKeyPool(GEMINI_API_KEYS)


def _is_quota_error(status_code: int, body_text: str) -> bool:
    if status_code == 429:
        return True
    lowered = body_text.lower()
    return any(marker in lowered for marker in _QUOTA_ERROR_MARKERS)


def _is_transient_error(status_code: int, body_text: str) -> bool:
    """503/500/504 (or an UNAVAILABLE/overloaded body) - a temporary
    service hiccup, not a quota problem. Callers must check
    _is_quota_error() first so a 429 body that happens to mention
    'unavailable' is never double-classified."""
    if status_code in _TRANSIENT_STATUS_CODES:
        return True
    lowered = body_text.lower()
    return any(marker in lowered for marker in _TRANSIENT_ERROR_MARKERS)


def _backoff_delay(attempt_index: int) -> float:
    """attempt_index 0 -> ~1s, 1 -> ~2s, 2 -> ~4s, plus a little jitter so
    many concurrent requests retrying the same outage don't all wake up
    and hammer Gemini at the exact same instant."""
    base = _TRANSIENT_BASE_DELAY_SECONDS * (2 ** attempt_index)
    return base + random.uniform(0, base * _TRANSIENT_JITTER_RATIO)


def _require_key():
    if not _gemini_pool.configured:
        raise AIError(
            "The server is missing GEMINI_API_KEY_1 (or GEMINI_API_KEY). "
            "Set at least one Gemini API key as an environment variable in Vercel."
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
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT, follow_redirects=True) as client:
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

# Serverless platforms (Vercel etc.) kill the whole function once its time
# limit is hit, and the browser just sees a dead connection ("Failed to
# fetch") rather than a clean error. These enrichment lookups (YouTube ID +
# blog link) are "nice to have", not core to the course, so every layer gets
# a short, hard timeout and the whole per-module enrichment step gets an
# overall budget - if it runs out, we just fall back to an empty string
# instead of blocking the response.
_LOOKUP_TIMEOUT = 5.0          # seconds per individual HTTP request
_ENRICH_BUDGET_PER_MODULE = 8.0  # seconds max spent finding video+blog for one module


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
    async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT, follow_redirects=True) as client:
        for search_fn in (_search_duckduckgo, _search_bing, _search_brave_api):
            try:
                link = await search_fn(client, query)
                if link:
                    return link
            except Exception as e:
                print(f"Error in {search_fn.__name__} for '{query}': {e}")
    return ""


async def _enrich_module(m: dict, fallback_topic: str) -> dict:
    """Attaches videoId + blogUrl to a module dict, in parallel, with a hard
    overall time budget so a slow/unreachable search engine can never stall
    course generation long enough to trip a serverless function timeout."""
    video_task = asyncio.create_task(get_youtube_video_id(m.get("videoQuery", fallback_topic)))
    blog_task = asyncio.create_task(
        get_blog_link(m.get("blogQuery") or m.get("videoQuery", fallback_topic))
    )
    try:
        video_id, blog_url = await asyncio.wait_for(
            asyncio.gather(video_task, blog_task, return_exceptions=True),
            timeout=_ENRICH_BUDGET_PER_MODULE,
        )
    except asyncio.TimeoutError:
        for t in (video_task, blog_task):
            t.cancel()
        video_id, blog_url = "", ""
    m["videoId"] = video_id if isinstance(video_id, str) else ""
    m["blogUrl"] = blog_url if isinstance(blog_url, str) else ""
    return m


def _escape_raw_control_chars_in_strings(s: str) -> str:
    """Gemini sometimes emits a literal newline/tab inside a JSON string
    value (easy to trigger once we ask for multi-line bulleted notes)
    instead of properly escaping it as \\n / \\t. Strict JSON treats a raw
    control character inside a string as the string ending there, which
    corrupts everything that follows and surfaces as a confusing
    "Expecting property name" error far later in the payload. This walks
    the text once, tracking whether we're inside a string literal, and
    escapes any offending raw control characters it finds there.
    """
    out = []
    in_string = False
    escaped = False
    for ch in s:
        if in_string:
            if escaped:
                out.append(ch)
                escaped = False
            elif ch == "\\":
                out.append(ch)
                escaped = True
            elif ch == '"':
                in_string = False
                out.append(ch)
            elif ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
    return "".join(out)


def _clean_json(raw: str) -> dict:
    raw = raw.strip()
    if "```" in raw:
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, re.IGNORECASE)
        if match:
            raw = match.group(1).strip()

    # Try the raw text first, then a sanitized version that fixes the most
    # common way Gemini's JSON output gets corrupted (raw control characters
    # left inside string values). Whichever succeeds first wins.
    candidates = [raw]
    sanitized = _escape_raw_control_chars_in_strings(raw)
    if sanitized != raw:
        candidates.append(sanitized)

    last_err = None
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict) and len(data) == 1 and isinstance(list(data.values())[0], dict):
                inner = list(data.values())[0]
                if "modules" in inner or "title" in inner:
                    return inner
            return data
        except Exception as e:
            last_err = e
            match = re.search(r"(\{[\s\S]*\})", candidate)
            if match:
                try:
                    return json.loads(match.group(1).strip())
                except Exception as e2:
                    last_err = e2

    # Last resort: the response was likely cut off mid-value (hit the token
    # budget before the model finished writing the string / closing the
    # braces). Rather than fail outright, try to repair it by closing the
    # dangling string and any still-open braces, then reparse. This can
    # recover a usable (if slightly truncated) value instead of erroring.
    repaired = _attempt_truncation_repair(candidates[-1])
    if repaired is not None:
        try:
            return json.loads(repaired)
        except Exception:
            pass

    raise AIError(f"Could not parse JSON response: {last_err}")


def _attempt_truncation_repair(s: str) -> str | None:
    """Best-effort repair of JSON that was truncated mid-stream (e.g. the
    model hit its output token limit before finishing). Walks the text
    tracking string/escape state and brace depth; if we're still inside an
    open string and/or have unbalanced braces at the end, closes them."""
    in_string = False
    escaped = False
    depth = 0
    for ch in s:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1

    if not in_string and depth == 0:
        return None  # nothing to repair; the failure was something else

    out = s
    if in_string:
        out += '"'
    out += "}" * max(depth, 0)
    return out


async def _call_gemini(prompt: str, schema: dict = None, max_tokens: int = 4000) -> str:
    _require_key()

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
    url = f"{GEMINI_BASE_URL}/{GEMINI_MODEL}:generateContent"

    projects = await _gemini_pool.ordered_available_projects()
    if not projects:
        raise AIError(
            "All configured Gemini projects are currently rate-limited. "
            "Please try again in a minute."
        )

    last_error_detail: str | None = None

    for idx, project in enumerate(projects):
        headers = {
            "x-goog-api-key": project.api_key,
            "content-type": "application/json",
        }
        print(f"Gemini request -> {project.label}")
        next_label = projects[idx + 1].label if idx + 1 < len(projects) else None

        # One client per project, reused across that project's own retry
        # attempts (no need to tear down and reconnect between retries of
        # the same project - only a fresh project gets a fresh client).
        async with httpx.AsyncClient(timeout=60) as client:
            resp = None
            rotate_reason = None  # "quota" | "transient" | None

            for attempt in range(_TRANSIENT_MAX_RETRIES + 1):  # 0 = first try
                try:
                    resp = await client.post(url, headers=headers, json=payload)
                except httpx.HTTPError as e:
                    # str(e) is sometimes empty for connection-level errors, so
                    # include the exception type/repr too - that's what
                    # actually shows up in Vercel's function logs. Connection
                    # failures aren't a per-project availability signal from
                    # Gemini itself, so they aren't retried against another
                    # project - same behavior as before.
                    detail = str(e) or repr(e)
                    raise AIError(f"Could not reach the Gemini API ({type(e).__name__}): {detail}")

                if resp.status_code == 200:
                    break  # success - handled after this inner loop

                last_error_detail = f"{project.label} -> {resp.status_code}: {resp.text[:300]}"

                if _is_quota_error(resp.status_code, resp.text):
                    print(f"{project.label} -> {resp.status_code} RESOURCE_EXHAUSTED")
                    rotate_reason = "quota"
                    resp = None
                    break  # no point retrying quota on the same project

                if _is_transient_error(resp.status_code, resp.text):
                    print(f"{project.label} -> {resp.status_code} UNAVAILABLE")
                    if attempt < _TRANSIENT_MAX_RETRIES:
                        delay = _backoff_delay(attempt)
                        print(f"Retrying {project.label} -> attempt {attempt + 2} (waiting {delay:.1f}s)")
                        await asyncio.sleep(delay)
                        continue  # retry the same project
                    print(f"{project.label} -> still unavailable")
                    rotate_reason = "transient"
                    resp = None
                    break

                # Permanent-looking error (invalid key, auth failure, malformed
                # request, invalid model, safety rejection, etc.) - don't
                # rotate projects for this, surface it directly like before.
                raise AIError(
                    f"Gemini API error ({resp.status_code}) using model '{GEMINI_MODEL}': {resp.text[:300]}"
                )

            if resp is None:
                if rotate_reason == "quota":
                    await _gemini_pool.mark_quota_exceeded(project)
                    print(f"{project.label} temporarily unavailable (quota)")
                else:
                    await _gemini_pool.mark_transient_unavailable(project)
                    print(f"{project.label} temporarily unavailable (overload)")
                if next_label:
                    print(f"Switching to {next_label}")
                continue  # try the next available project

            print(f"{project.label} -> success")
            await _gemini_pool.mark_active(project)

            data = resp.json()
            candidates = data.get("candidates", [])
            if not candidates:
                raise AIError("No response candidates returned by Gemini.")

            parts = candidates[0].get("content", {}).get("parts", [])
            text = "".join(part.get("text", "") for part in parts if "text" in part)
            if not text:
                raise AIError("No text returned from Gemini.")
            return text

    # Every available project either hit its quota or stayed unavailable
    # through its retries. Log the raw detail server-side for debugging but
    # never expose it to the caller/frontend - just a clean, actionable
    # message (same response shape callers already expect from AIError).
    if last_error_detail:
        print(f"All Gemini projects failed. Last error: {last_error_detail}")
    raise AIError(
        "The AI service is temporarily unavailable. Please try again in a minute."
    )


async def generate_course(topic: str) -> dict:
    prompt = (
        f"Design a high-quality 4-module short course on: {topic}.\n"
        "Requirements:\n"
        "- Title: concise course title.\n"
        "- Description: 20 words or fewer.\n"
        "- Exactly 4 modules ordered from foundational to advanced.\n"
        "- Module notes: 80-130 words in markdown, structured as exactly 3 "
        "top-level bullet points (each line starting with '- '), one key "
        "idea per bullet. Under EACH top-level bullet, add 2 nested "
        "sub-bullets indented with exactly 2 spaces (each line starting "
        "with '  - ') giving a supporting detail, example, or explanation "
        "for that point.\n"
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

    # Enrich all modules with a real YouTube video ID + blog/article URL at
    # the same time (previously this ran one module at a time, each doing up
    # to ~6 sequential scraping requests - easily 100s+ for a 4-module
    # course, which is enough to trip a serverless function timeout and
    # surface as "Failed to fetch" on the client).
    await asyncio.gather(*(_enrich_module(m, topic) for m in parsed["modules"]))

    return parsed


async def generate_module(course_title: str, lesson_topic: str, difficulty: str = None) -> dict:
    prompt = (
        f'Course: "{course_title}". Write the lesson module for: "{lesson_topic}".\n'
        "Notes: 80-130 words in markdown, structured as exactly 3 top-level "
        "bullet points (each line starting with '- '), one key idea per "
        "bullet. Under EACH top-level bullet, add 2 nested sub-bullets "
        "indented with exactly 2 spaces (each line starting with '  - ') "
        "giving a supporting detail, example, or explanation for that "
        "point.\n"
        "Also include a blog query: a specific search phrase to find one good "
        "written article or blog post on this subtopic (8 words or fewer)."
    )
    # Difficulty steering (Feature 9): an extra instruction appended only when
    # requested. Omitted entirely (byte-identical prompt) when difficulty is
    # None, so the default regeneration path is unchanged.
    if difficulty == "simpler":
        prompt += (
            "\nDifficulty: write this for a complete beginner — plain, simple "
            "language; short sentences; everyday analogies; explain any term "
            "the moment it appears."
        )
    elif difficulty == "advanced":
        prompt += (
            "\nDifficulty: write this for an advanced learner — go deeper "
            "technically, use precise domain terminology, and cover "
            "edge cases, caveats, or nuances a beginner version would skip."
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
    await _enrich_module(parsed, lesson_topic)
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


async def generate_flashcards(module_title: str, module_notes: str) -> dict:
    """Flashcards for a lesson — same _call_gemini + _clean_json pattern as
    the other generators (Feature 5)."""
    prompt = (
        f'Lesson title: "{module_title}"\n'
        f'Lesson notes:\n{module_notes}\n\n'
        "Create exactly 6 flashcards to help a learner memorize the key ideas "
        "of this lesson. Each card has a short 'front' (a question or term, "
        "12 words or fewer) and a concise 'back' answer (30 words or fewer)."
    )
    fc_schema = {
        "type": "OBJECT",
        "properties": {
            "cards": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "front": {"type": "STRING"},
                        "back": {"type": "STRING"},
                    },
                    "required": ["front", "back"],
                },
            },
        },
        "required": ["cards"],
    }

    raw = await _call_gemini(prompt, schema=fc_schema, max_tokens=2000)
    parsed = _clean_json(raw)

    if not isinstance(parsed, dict) or not isinstance(parsed.get("cards"), list) or not parsed["cards"]:
        raise AIError("Incomplete flashcard data returned by the model.")

    # Keep only well-formed cards so one malformed entry can never break the
    # flip-card UI downstream.
    cards = [
        {"front": str(c.get("front", "")).strip(), "back": str(c.get("back", "")).strip()}
        for c in parsed["cards"]
        if isinstance(c, dict) and str(c.get("front", "")).strip() and str(c.get("back", "")).strip()
    ]
    if not cards:
        raise AIError("No usable flashcards returned by the model.")
    return {"cards": cards}


async def answer_question(module_title: str, module_notes: str, question: str) -> str:
    """Answer a learner's question in the context of a lesson's notes.
    Stateless — nothing is stored (Feature 6)."""
    prompt = (
        f'Lesson title: "{module_title}"\n'
        f'Lesson notes:\n{module_notes}\n\n'
        f'A learner asks: "{question}"\n\n'
        "Answer in at most 120 words of plain text, friendly and direct. Base "
        "the answer on the lesson notes where possible; if the notes don't "
        "cover it, say so briefly and answer from general knowledge."
    )
    answer_schema = {
        "type": "OBJECT",
        "properties": {"answer": {"type": "STRING"}},
        "required": ["answer"],
    }

    raw = await _call_gemini(prompt, schema=answer_schema, max_tokens=800)
    parsed = _clean_json(raw)

    answer = parsed.get("answer") if isinstance(parsed, dict) else None
    if not answer or not str(answer).strip():
        raise AIError("No answer returned by the model.")
    return str(answer).strip()


async def generate_diagram(module_title: str, module_notes: str) -> dict:
    """Ask Gemini for a Mermaid.js diagram for a lesson (Feature 10).

    The model decides whether a diagram helps; an empty 'diagram' string
    means "no diagram needed" and nothing is stored. Returns
    {"diagram": str, "explanation": str}."""
    prompt = (
        f'Lesson title: "{module_title}"\n'
        f'Lesson notes:\n{module_notes}\n\n'
        "Decide whether a Mermaid.js diagram would genuinely help a learner "
        "understand this lesson (a flow, cycle, hierarchy, relationship map, "
        "timeline, or breakdown). If yes, return ONE diagram in the 'diagram' "
        "field as pure Mermaid syntax — no code fences, no commentary — that "
        "renders with mermaid.js v10 (flowchart TD, sequenceDiagram, "
        "classDiagram, stateDiagram-v2, erDiagram, mindmap, timeline, or "
        "pie). Keep node labels short (4 words or fewer). Do not put double "
        "quote characters anywhere in the diagram, including inside node "
        "labels (write A[Fast adjective] not A[\"Fast adjective\"]) — this "
        "diagram is embedded in a JSON string and a stray quote breaks it. "
        "Avoid other special characters that break Mermaid parsing, and "
        "keep it under 25 lines. "
        "If a diagram would NOT genuinely help, return an empty string in "
        "'diagram'. Put one short sentence describing the diagram (or why "
        "none is needed) in 'explanation'."
    )
    diagram_schema = {
        "type": "OBJECT",
        "properties": {
            "diagram": {"type": "STRING"},
            "explanation": {"type": "STRING"},
        },
        "required": ["diagram", "explanation"],
    }

    raw = await _call_gemini(prompt, schema=diagram_schema, max_tokens=2500)
    parsed = None
    try:
        parsed = _clean_json(raw)
    except AIError:
        try:
            # Diagram text that itself contains ``` fences (models love
            # wrapping mermaid in code fences even when told not to) trips
            # _clean_json's own fence-stripping, which would swallow
            # everything between the fences. Neutralize the backticks into
            # JSON unicode escapes — they decode back to the exact same
            # characters after parsing, but no longer look like fences to
            # _clean_json.
            neutralized = raw.replace("```", "\\u0060\\u0060\\u0060")
            parsed = _clean_json(neutralized)
        except AIError:
            # Still not strict JSON — most likely the model left a literal,
            # unescaped double-quote inside the diagram text (e.g. Mermaid
            # label syntax like A["text"]), which prematurely ends the JSON
            # string from a strict parser's point of view even though the
            # response is otherwise complete. Since we know the exact shape
            # of this response ({"diagram": ..., "explanation": ...}), fall
            # back to pulling each field out positionally instead of
            # requiring the whole blob to be valid JSON.
            diagram_loose = _extract_json_string_field(raw, "diagram", "explanation")
            explanation_loose = _extract_json_string_field(raw, "explanation", None)
            if diagram_loose is None and explanation_loose is None:
                raise
            parsed = {"diagram": diagram_loose or "", "explanation": explanation_loose or ""}

    diagram = ""
    explanation = ""
    if isinstance(parsed, dict):
        diagram = str(parsed.get("diagram") or "").strip()
        explanation = str(parsed.get("explanation") or "").strip()
        # Defensive: strip ```mermaid fences if the model added them anyway.
        fence = re.search(r"```(?:mermaid)?\s*([\s\S]*?)```", diagram, re.IGNORECASE)
        if fence:
            diagram = fence.group(1).strip()

    return {"diagram": diagram, "explanation": explanation}


def _extract_json_string_field(raw: str, key: str, next_key: str | None) -> str | None:
    """Loosely pull a string value for `key` out of a JSON-ish blob,
    tolerating unescaped/unbalanced quotes *inside* the value — which trips
    a strict JSON parser but is common when the value itself legitimately
    contains quoted text (e.g. Mermaid node labels written as A["text"]).

    Finds `"key": "`, then takes everything up to the boundary right before
    `"next_key":` (or to the end of the blob if next_key is None / not
    found), trimming a trailing dangling quote/brace/comma left over from
    the outer JSON structure. Returns None if `key` isn't found at all."""
    start_match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', raw)
    if not start_match:
        return None
    start = start_match.end()

    value = raw[start:]
    if next_key:
        end_match = re.search(r'"\s*,?\s*"' + re.escape(next_key) + r'"\s*:', value)
        if end_match:
            value = value[: end_match.start()]

    value = re.sub(r'"?\s*\}*\s*$', "", value)
    value = (
        value.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )
    return value.strip()


async def explain_like_im_five(module_title: str, module_notes: str) -> str:
    """"Explain Like I'm 5" resummaries (Feature 11): returns a simpler
    version of the notes without overwriting the stored ones."""
    prompt = (
        f'Lesson title: "{module_title}"\n'
        f'Lesson notes:\n{module_notes}\n\n'
        "Rewrite these lesson notes at a much simpler reading level, as if "
        "explaining to a curious five-year-old: very short sentences, "
        "everyday words only, and one simple analogy that makes the core idea "
        "click. 90 words or fewer, in markdown with at most 3 bullet points."
    )
    eli5_schema = {
        "type": "OBJECT",
        "properties": {"summary": {"type": "STRING"}},
        "required": ["summary"],
    }

    raw = await _call_gemini(prompt, schema=eli5_schema, max_tokens=800)
    parsed = _clean_json(raw)

    summary = parsed.get("summary") if isinstance(parsed, dict) else None
    if not summary or not str(summary).strip():
        raise AIError("No simplified summary returned by the model.")
    return str(summary).strip()
