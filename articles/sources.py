"""The feeds we read.

`full_text` marks feeds that ship the whole article in the XML, so those need
no second request. The rest give a link and we fetch the page.
"""

FEEDS = [
    {"slug": "hn", "name": "Hacker News", "url": "https://hnrss.org/frontpage?points=150", "full_text": False},
    {"slug": "devto", "name": "dev.to", "url": "https://dev.to/feed", "full_text": True},
    {"slug": "netflix", "name": "Netflix Tech Blog", "url": "https://netflixtechblog.com/feed", "full_text": True},
    {"slug": "stripe", "name": "Stripe", "url": "https://stripe.com/blog/feed.rss", "full_text": False},
    {"slug": "github", "name": "GitHub Blog", "url": "https://github.blog/feed/", "full_text": False},
    {"slug": "cloudflare", "name": "Cloudflare", "url": "https://blog.cloudflare.com/rss/", "full_text": False},
    {"slug": "shopify", "name": "Shopify Engineering", "url": "https://shopify.engineering/blog.atom", "full_text": False},
    {"slug": "aws", "name": "AWS Architecture", "url": "https://aws.amazon.com/blogs/architecture/feed/", "full_text": False},
]

# Link targets that are never a readable article: binaries, video, social posts,
# code hosts, and Hacker News' own discussion pages (Ask HN, Show HN threads).
SKIP_HOSTS = {
    "news.ycombinator.com", "twitter.com", "x.com", "youtube.com", "www.youtube.com",
    "youtu.be", "github.com", "gist.github.com", "reddit.com", "www.reddit.com",
    "arxiv.org", "docs.google.com", "open.spotify.com",
}
SKIP_SUFFIXES = (".pdf", ".zip", ".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".gif", ".svg")
