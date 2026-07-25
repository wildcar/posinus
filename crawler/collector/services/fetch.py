import gzip
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree

import feedparser
import trafilatura
from charset_normalizer import from_bytes
from django.conf import settings
from lxml import html

from collector.models import Source, SourceEndpoint

_ROBOTS_CACHE = {}


def validate_public_url(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))}
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve host {parts.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError(f"Private or reserved address is forbidden: {address}")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass
class FetchResult:
    url: str
    status: int
    body: bytes
    headers: dict = field(default_factory=dict)


def decompress_gzip_body(body: bytes) -> bytes:
    return gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body


def decode_html(result: FetchResult) -> str:
    content_type = result.headers.get("Content-Type", "")
    match = re.search(r"charset=([\w.-]+)", content_type, re.I)
    if match:
        try:
            return result.body.decode(match.group(1), errors="replace")
        except LookupError:
            pass
    detected = from_bytes(result.body).best()
    return str(detected) if detected is not None else result.body.decode("utf-8", errors="replace")


def allowed_by_robots(url: str) -> bool:
    validate_public_url(url)
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    cached = _ROBOTS_CACHE.get(robots_url)
    if cached and time.monotonic() - cached[0] < 600:
        return cached[1]
    parser = urllib.robotparser.RobotFileParser(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": settings.NEWSCRAWLER_USER_AGENT})
        with urllib.request.build_opener(SafeRedirectHandler()).open(request, timeout=10) as response:
            parser.parse(response.read(1_000_000).decode("utf-8", errors="replace").splitlines())
        allowed = parser.can_fetch(settings.NEWSCRAWLER_USER_AGENT, url)
    except urllib.error.HTTPError as exc:
        allowed = exc.code == 404
    except (urllib.error.URLError, TimeoutError, ValueError):
        allowed = False
    _ROBOTS_CACHE[robots_url] = (time.monotonic(), allowed)
    return allowed


def fetch_url(url: str, *, etag="", last_modified="", playwright=False, delay=0.0) -> FetchResult:
    validate_public_url(url)
    if not allowed_by_robots(url):
        raise PermissionError(f"robots.txt forbids {url}")
    if delay:
        time.sleep(delay)
    if playwright:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as engine:
                browser = engine.chromium.launch(headless=True)
                context = browser.new_context(user_agent=settings.NEWSCRAWLER_USER_AGENT)
                def safe_route(route):
                    request_url = route.request.url
                    if urlsplit(request_url).scheme not in {"http", "https"}:
                        route.continue_()
                        return
                    try:
                        validate_public_url(request_url)
                        route.continue_()
                    except ValueError:
                        route.abort()
                context.route("**/*", safe_route)
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                content = page.content().encode("utf-8")
                status = response.status if response else 200
                final_url = page.url
                validate_public_url(final_url)
                context.close()
                browser.close()
                return FetchResult(final_url, status, content, {})
        except Exception as exc:
            raise RuntimeError(f"Playwright fetch failed: {exc}") from exc
    headers = {"User-Agent": settings.NEWSCRAWLER_USER_AGENT, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(SafeRedirectHandler()).open(request, timeout=30) as response:
            # Header names may arrive in any case, so rely on the gzip magic bytes instead.
            body = decompress_gzip_body(response.read(10_000_000))
            response_headers = dict(response.headers.items())
            return FetchResult(response.geturl(), response.status, body, response_headers)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return FetchResult(url, 304, b"", dict(exc.headers.items()))
        raise


def parse_datetime(value):
    if not value:
        return None
    if hasattr(value, "tm_year"):
        return datetime(*value[:6], tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            return parsedate_to_datetime(str(value))
        except (TypeError, ValueError):
            return None


def discover_endpoints(source: Source, body: bytes, page_url: str):
    document = html.fromstring(str(from_bytes(body).best() or body.decode("utf-8", errors="replace")), base_url=page_url)
    found = []
    for node in document.xpath("//link[@href]"):
        mime = (node.get("type") or "").lower()
        rel = (node.get("rel") or "").lower()
        if "alternate" in rel and ("rss" in mime or "atom" in mime):
            found.append((SourceEndpoint.Kind.RSS, urljoin(page_url, node.get("href"))))
    parts = urlsplit(source.base_url)
    found.append((SourceEndpoint.Kind.SITEMAP, f"{parts.scheme}://{parts.netloc}/sitemap.xml"))
    found.append((SourceEndpoint.Kind.HTML, page_url))
    priorities = {SourceEndpoint.Kind.RSS: 10, SourceEndpoint.Kind.SITEMAP: 20, SourceEndpoint.Kind.HTML: 30}
    for kind, url in found:
        SourceEndpoint.objects.get_or_create(source=source, url=url, defaults={"kind": kind, "priority": priorities[kind]})
    return found


def candidate_urls(endpoint: SourceEndpoint, result: FetchResult):
    if result.status == 304:
        return []
    if endpoint.kind == SourceEndpoint.Kind.RSS:
        feed = feedparser.parse(result.body)
        return [(entry.get("link"), parse_datetime(entry.get("published_parsed") or entry.get("updated_parsed"))) for entry in feed.entries if entry.get("link")]
    if endpoint.kind == SourceEndpoint.Kind.SITEMAP:
        body = gzip.decompress(result.body) if result.body[:2] == b"\x1f\x8b" else result.body
        root = ElementTree.fromstring(body)
        urls = []
        for element in root.iter():
            if element.tag.endswith("loc") and element.text:
                urls.append((element.text.strip(), None))
        return urls[-500:]
    document = html.fromstring(decode_html(result), base_url=result.url)
    return [(urljoin(result.url, value), None) for value in document.xpath("//a/@href")][:500]


def extract_article(source: Source, result: FetchResult, published_at=None):
    document = html.fromstring(decode_html(result), base_url=result.url)
    config = source.adapter_config or {}
    title = ""
    body = ""
    title_selector = config.get("title_selector")
    body_selector = config.get("body_selector")
    if title_selector:
        nodes = document.cssselect(title_selector)
        title = " ".join(nodes[0].itertext()).strip() if nodes else ""
    if body_selector:
        nodes = document.cssselect(body_selector)
        body = "\n".join(" ".join(node.itertext()).strip() for node in nodes).strip()
    extracted = trafilatura.extract(result.body, output_format="json", with_metadata=True, include_comments=False, include_tables=False)
    data = {}
    if extracted:
        import json
        data = json.loads(extracted)
    title = title or data.get("title") or ""
    body = body or data.get("text") or ""
    canonical = data.get("url") or result.url
    language = data.get("language") or document.get("lang") or ""
    author = data.get("author") or ""
    date = published_at or parse_datetime(data.get("date"))
    links = document.xpath("//a[@href]/@href")
    return {"url": result.url, "canonical_url": canonical, "title": title, "body": body, "language": language, "author": author, "published_at": date, "metadata": {k: v for k, v in data.items() if k not in {"text", "title", "author", "date", "url"}}, "links": links}


def url_matches(source: Source, url: str) -> bool:
    lower_url = url.lower()
    if any(marker in lower_url for marker in ("/login", "/signin", "/sign-in", "/subscribe", "/paywall", "/captcha")):
        return False
    include = source.include_patterns or []
    exclude = source.exclude_patterns or []
    if include and not any(re.search(pattern, url) for pattern in include):
        return False
    return not any(re.search(pattern, url) for pattern in exclude)
