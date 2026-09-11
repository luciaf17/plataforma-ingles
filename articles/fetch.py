"""Pull articles from the feeds in sources.py and store the readable ones.

Everything here is best effort: a feed that is down, a page that refuses to be
read or an article in the wrong language is skipped and logged, never raised.
A reading lesson still works with no articles at all — the planner falls back
to writing its own text.
"""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlparse

import feedparser
import trafilatura
from django.db import IntegrityError
from django.utils import timezone

from .models import Article
from .sources import FEEDS, SKIP_HOSTS, SKIP_SUFFIXES

log = logging.getLogger("articles")

# Long enough to be worth a lesson, short enough that the excerpt is the top of
# a real piece and not a whole book.
MIN_WORDS = 250
MAX_WORDS = 8000
# Very common English function words; Spanish and German prose score far lower.
STOPWORDS = {
    "the", "and", "to", "of", "a", "in", "is", "it", "that", "for", "on", "with", "as",
    "was", "we", "you", "this", "be", "are", "not", "have", "but", "from", "or", "an", "they",
}
ENGLISH_THRESHOLD = 0.20
PER_FEED = 8


def looks_english(text):
    tokens = [t.strip(".,:;!?()[]\"'").lower() for t in text.split()[:400]]
    tokens = [t for t in tokens if t]
    if not tokens:
        return False
    return sum(1 for t in tokens if t in STOPWORDS) / len(tokens) >= ENGLISH_THRESHOLD


def is_readable_url(url):
    if not url or not url.startswith("http"):
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in SKIP_HOSTS or f"www.{host}" in SKIP_HOSTS:
        return False
    return not parsed.path.lower().endswith(SKIP_SUFFIXES)


def download(url):
    """One HTTP GET, through trafilatura so we inherit its headers and timeouts.

    feedparser's own fetching is refused by some of these hosts; this is not.
    """
    try:
        return trafilatura.fetch_url(url)
    except Exception as exc:  # noqa: BLE001 - network layer raises many things
        log.warning("could not fetch %s: %s", url, exc)
        return None


def entry_html(entry):
    """The article body carried inside the feed, when there is one."""
    content = entry.get("content")
    if isinstance(content, list) and content:
        return content[0].get("value", "")
    detail = entry.get("summary_detail")
    if isinstance(detail, dict):
        return detail.get("value", "")
    return ""


def paragraphs(text):
    """One blank line between paragraphs.

    trafilatura separates them with a single newline; the rest of the app (and
    the reading screen) splits on blank lines, so normalise here at the source.
    """
    return "\n\n".join(line.strip() for line in text.splitlines() if line.strip())


def extract(html, url):
    try:
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=False) or ""
    except Exception as exc:  # noqa: BLE001
        log.warning("could not extract %s: %s", url, exc)
        return ""
    return paragraphs(text)


def published(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=dt_timezone.utc)


def article_text(entry, feed):
    """Body text for one entry: from the feed when it carries it, else the page."""
    if feed["full_text"]:
        text = extract(entry_html(entry), entry.get("link", ""))
        if len(text.split()) >= MIN_WORDS:
            return text
    html = download(entry.get("link", ""))
    return extract(html, entry.get("link", "")) if html else ""


def fetch_feed(feed):
    """Entries for one feed, newest first, or an empty list if it is unavailable."""
    raw = download(feed["url"])
    if not raw:
        log.warning("feed %s is unavailable", feed["slug"])
        return []
    return feedparser.parse(raw).entries[:PER_FEED]


def store(entry, feed):
    """Save one entry as an Article. Returns the reason it was skipped, or None."""
    url = entry.get("link", "")
    if not is_readable_url(url):
        return "not an article"
    if Article.objects.filter(url=url).exists():
        return "already stored"
    title = (entry.get("title") or "").strip()
    if not title:
        return "no title"

    text = article_text(entry, feed)
    words = len(text.split())
    if words < MIN_WORDS:
        return f"too short ({words} words)"
    if words > MAX_WORDS:
        return f"too long ({words} words)"
    if not looks_english(text):
        return "not English"

    try:
        Article.objects.create(
            source=feed["slug"], source_name=feed["name"], url=url, title=title[:300],
            author=(entry.get("author") or "").strip()[:160], text=text, word_count=words,
            published_at=published(entry),
        )
    except IntegrityError:
        # Two feeds can carry the same link; the unique url settles it.
        return "already stored"
    return None


def refresh(feeds=None):
    """Read every feed once. Returns {"stored": n, "skipped": n, "feeds": n}."""
    feeds = feeds if feeds is not None else FEEDS
    stored = skipped = reached = 0
    for feed in feeds:
        entries = fetch_feed(feed)
        if entries:
            reached += 1
        for entry in entries:
            reason = store(entry, feed)
            if reason is None:
                stored += 1
            else:
                skipped += 1
                log.debug("skipped %s: %s", entry.get("link", ""), reason)
        log.info("feed %s: %d entries", feed["slug"], len(entries))
    log.info("articles refreshed: %d stored, %d skipped, %d/%d feeds reached", stored, skipped, reached, len(feeds))
    return {"stored": stored, "skipped": skipped, "feeds": reached}


def prune(keep_days=60):
    """Drop old articles nobody read, so the table does not grow forever."""
    cutoff = timezone.now() - timedelta(days=keep_days)
    deleted, _ = Article.objects.filter(fetched_at__lt=cutoff, uses__isnull=True).delete()
    return deleted
