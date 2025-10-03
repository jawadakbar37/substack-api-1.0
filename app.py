from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Any
import hashlib, httpx
from bs4 import BeautifulSoup
from urllib.parse import urlparse

from substack_api.post import Post  # requires substack-api
app = FastAPI(title="Substack Proxy")

class PostOut(BaseModel):
    url: str
    canonical_url: Optional[str] = None
    title: Optional[str] = None
    author: Optional[str] = None
    publication: Optional[str] = None
    published_at: Optional[str] = None
    hero_image: Optional[str] = None
    html: Optional[str] = None
    text: Optional[str] = None
    sha256: Optional[str] = None
    source: str

@app.get("/")
def root(request: Request):
    q = request.query_params.get("url")
    if q:
        # optional convenience: redirect /?url=... to /post?url=...
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=f"/post?url={q}")
    return {"ok": True, "routes": ["/", "/healthz", "/post"]}

@app.get("/healthz")
def healthz():
    return {"ok": True}

def _readability_extract(html: str):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    article = soup.find("article") or soup.find("main") or soup.body
    img = (article and article.find("img")) or soup.find("img")
    hero = img.get("src") if img and img.get("src") else None
    text = article.get_text("\n", strip=True) if article else soup.get_text("\n", strip=True)
    return text, title, hero

@app.get("/post", response_model=PostOut)
def get_post(url: str = Query(..., description="Public Substack post URL")):
    # 1) Normalize by following redirects (handles custom domains -> canonical)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; SubstackProxy/1.0)"}
    try:
        with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
            head = client.get(url)  # GET to also work for sites not supporting HEAD
            final_url = str(head.url)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"URL normalize failed: {e}")

    # 2) Try substack_api first
    try:
        p = Post(final_url)  # library handles many cases, but not all custom domains
        md = p.get_metadata()
        html = p.get_content(as_html=True)
        text = p.get_content(as_html=False)
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
        return PostOut(
            url=final_url,
            canonical_url=md.get("canonical_url") or final_url,
            title=md.get("title"),
            author=md.get("author"),
            publication=md.get("publication"),
            published_at=md.get("published_at"),
            hero_image=md.get("hero_image"),
            html=html,
            text=text,
            sha256=sha,
            source="substack_api"
        )
    except Exception as e:
        # 3) Fallback: generic fetch + readability (works for custom domains)
        try:
            with httpx.Client(timeout=30.0, headers=headers, follow_redirects=True) as client:
                r = client.get(final_url)
                r.raise_for_status()
                html = r.text
                text, title_guess, hero = _readability_extract(html)
                sha = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None
                return PostOut(
                    url=final_url,
                    canonical_url=str(r.url),
                    title=title_guess,
                    author=None,
                    publication=urlparse(str(r.url)).hostname,
                    published_at=None,
                    hero_image=hero,
                    html=html,
                    text=text,
                    sha256=sha,
                    source="generic-fetch"
                )
        except Exception as e2:
            # keep 502 but include both error hints
            raise HTTPException(
                status_code=502,
                detail=f"substack_api failed: {e}; fallback fetch failed: {e2}"
            )
