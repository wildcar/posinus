import gzip
from types import SimpleNamespace

import pytest

from collector.models import Source, SourceEndpoint
from collector.services.fetch import FetchResult, candidate_urls, decompress_gzip_body, extract_article


@pytest.fixture
def rss_result():
    return FetchResult("https://news.example/feed.xml", 200, b"""<?xml version='1.0'?>
    <rss version='2.0'><channel><title>News</title><item>
    <title>A useful story</title><link>https://news.example/story</link>
    <pubDate>Mon, 13 Jul 2026 10:00:00 GMT</pubDate>
    </item></channel></rss>""")


def test_rss_candidates(rss_result):
    endpoint = SimpleNamespace(kind=SourceEndpoint.Kind.RSS)
    candidates = candidate_urls(endpoint, rss_result)
    assert candidates[0][0] == "https://news.example/story"
    assert candidates[0][1].year == 2026


def test_gzip_body_decompressed_by_magic_bytes():
    page = b"<html><body><a href='https://news.example/story'>x</a></body></html>"
    assert decompress_gzip_body(gzip.compress(page)) == page
    assert decompress_gzip_body(page) == page
    assert decompress_gzip_body(b"") == b""


def test_gzipped_sitemap_candidates():
    endpoint = SimpleNamespace(kind=SourceEndpoint.Kind.SITEMAP)
    body = gzip.compress(b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'><url><loc>https://news.example/a</loc></url></urlset>")
    assert candidate_urls(endpoint, FetchResult("https://news.example/sitemap.xml.gz", 200, body)) == [("https://news.example/a", None)]


@pytest.mark.django_db
def test_custom_css_extraction_multilingual():
    source = Source.objects.create(
        name="News", base_url="https://news.example/", domain="news.example",
        adapter_config={"title_selector": "h1.headline", "body_selector": "article.story"},
    )
    body = "Это хорошая новость о новом парке и работе волонтеров. " * 10
    page = f"""<html lang='ru'><head><title>Wrong title</title></head><body>
    <h1 class='headline'>Открыт новый парк</h1><article class='story'><p>{body}</p></article>
    <a href='https://another.example/source'>Источник</a></body></html>""".encode()
    article = extract_article(source, FetchResult("https://news.example/story", 200, page))
    assert article["title"] == "Открыт новый парк"
    assert "волонтеров" in article["body"]
    assert article["language"] == "ru"
    assert article["links"] == ["https://another.example/source"]

