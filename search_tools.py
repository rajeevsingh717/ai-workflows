"""Web + Reddit search helpers."""
import os
import httpx


def tavily_search(query: str, max_results: int = 5) -> list[dict]:
    """Search the web via Tavily. Falls back to DuckDuckGo if no API key."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return _ddg_fallback(query, max_results)
    try:
        r = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "max_results": max_results,
                "search_depth": "advanced",
                "include_answer": False,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        return [
            {
                "title": h.get("title", ""),
                "url": h.get("url", ""),
                "content": h.get("content", ""),
            }
            for h in data.get("results", [])
        ]
    except Exception as e:
        print(f"  ! Tavily error ({e}); falling back to DuckDuckGo")
        return _ddg_fallback(query, max_results)


def _ddg_fallback(query: str, max_results: int) -> list[dict]:
    try:
        from ddgs import DDGS
        with DDGS() as ddg:
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "content": r.get("body", ""),
                }
                for r in ddg.text(query, max_results=max_results)
            ]
    except Exception as e:
        print(f"  ! DuckDuckGo error: {e}")
        return []


REDDIT_HEADERS = {"User-Agent": "deep-research-langgraph/0.1"}


def reddit_search(query: str, limit: int = 5, fetch_comments_for_top: int = 2) -> list[dict]:
    """Search Reddit via DuckDuckGo (site:reddit.com) — no API key needed."""
    try:
        from ddgs import DDGS
        with DDGS() as ddg:
            raw = list(ddg.text(f"{query} site:reddit.com", max_results=limit))
        posts = []
        for r in raw:
            url = r.get("href", "")
            subreddit = ""
            parts = url.split("/r/")
            if len(parts) > 1:
                subreddit = parts[1].split("/")[0]
            posts.append({
                "title": r.get("title", ""),
                "url": url,
                "subreddit": subreddit,
                "score": 0,
                "content": r.get("body", ""),
            })
        for p in posts[:fetch_comments_for_top]:
            if "/comments/" in p["url"]:
                comments = _fetch_reddit_comments(p["url"])
                if comments:
                    p["content"] = (p["content"] + "\n\nTOP COMMENTS:\n" + comments).strip()
        return posts
    except Exception as e:
        print(f"  ! Reddit search error: {e}")
        return []


def _fetch_reddit_comments(post_url: str, n: int = 5) -> str:
    try:
        r = httpx.get(post_url + ".json", headers=REDDIT_HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
        if len(data) < 2:
            return ""
        children = data[1].get("data", {}).get("children", [])
        lines = []
        for c in children[:n]:
            body = c.get("data", {}).get("body", "")
            if body and body not in ("[deleted]", "[removed]"):
                lines.append(f"- {body[:400]}")
        return "\n".join(lines)
    except Exception:
        return ""
