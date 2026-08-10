"""Fetch a business's own website and reduce it to readable text.

This is the front door of the product: §1 of the north star is "paste your
website URL and get a campaign", and until now the Research agent was handed
the URL as a string it had no way to visit.

Deliberately a plain HTTP fetch rather than a headless browser. Small business
sites — WordPress, Squarespace, Wix, Shopify — render their copy server-side,
and a browser would mean Playwright on a 512MB instance for a minority of
JavaScript-only sites. If real sites turn out to fail, Bright Data's Web
Unlocker is an HTTP API that slots in behind this same interface without
dragging a browser onto the box.

The security note matters more than the parsing. A user hands us a URL and the
server fetches it, which is a server-side request forgery hole by default: a
link to 169.254.169.254 or 127.0.0.1 asks our own infrastructure to read its
metadata service or internal ports and hand the result back. Everything below
about address ranges is there for that.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

log = logging.getLogger(__name__)


class SiteError(RuntimeError):
    """The site could not be read. The message is shown to the user."""


TIMEOUT = 20
MAX_BYTES = 2_000_000        # a marketing site's HTML; anything larger is not copy
MAX_REDIRECTS = 4
MAX_PAGES = 4                # home page plus a few obvious ones

# Presented as a browser because some hosts serve a stub or a block page to
# unknown clients. Honest about who we are in the comment; the header is a
# compatibility measure, not a disguise for evading a refusal.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36 Campaignist/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Pages that usually carry the words a marketer needs: what they sell, who for,
# and what it costs.
WORTH_READING = ("/about", "/about-us", "/services", "/menu", "/pricing",
                 "/prices", "/shop", "/products", "/what-we-do")


def _is_public_address(host: str) -> bool:
    """Does this hostname resolve only to addresses on the public internet?

    Checked against every resolved address, not just the first: a name can
    return one public and one private address, and picking the first would let
    the private one through on retry.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False

    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return False
    return True


def normalise(raw: str) -> str:
    """Turn what someone types into a URL, or raise with a readable reason."""
    candidate = (raw or "").strip()
    if not candidate:
        raise SiteError("Enter your website address.")
    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise SiteError("Only http and https addresses can be read.")
    if not parsed.hostname or "." not in parsed.hostname:
        raise SiteError(f"{raw!r} does not look like a website address.")
    if not _is_public_address(parsed.hostname):
        # Deliberately vague to the user, specific in the log: the difference
        # between "does not resolve" and "resolves to a private address" is
        # itself information about our network.
        log.warning("[site] refused non-public host %r", parsed.hostname)
        raise SiteError("That address could not be reached.")
    return parsed.geturl()


class _Text(HTMLParser):
    """Collect visible text, the title, and same-site links."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[str] = []
        self.title = ""
        self.description = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
        if tag == "meta":
            a = dict(attrs)
            if a.get("name", "").lower() in ("description", "og:description") or \
               a.get("property", "").lower() == "og:description":
                self.description = self.description or (a.get("content") or "")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = self.title or text
        elif not self._skip_depth:
            self.chunks.append(text)


def _fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT,
                        allow_redirects=True, stream=True)
    if resp.status_code >= 400:
        raise SiteError(f"The site returned {resp.status_code}.")

    # Redirects can land somewhere private even when the first hop was public,
    # so the final destination is checked too.
    final = urlparse(resp.url)
    if final.hostname and not _is_public_address(final.hostname):
        log.warning("[site] %s redirected to non-public host %r", url, final.hostname)
        raise SiteError("That address could not be reached.")

    if "html" not in resp.headers.get("Content-Type", "").lower():
        raise SiteError("That address is not a web page.")

    body = resp.raw.read(MAX_BYTES, decode_content=True) or b""
    return body.decode(resp.encoding or "utf-8", errors="replace")


def _clean(chunks: list[str]) -> str:
    text = " ".join(chunks)
    return re.sub(r"\s+", " ", text).strip()


def read(raw_url: str) -> dict:
    """Read a site and return what a marketer would need from it.

    Follows a few obvious internal pages, because a home page is often a
    slogan and a photograph while /about and /menu carry the words that say
    what the business actually does.
    """
    url = normalise(raw_url)
    origin = urlparse(url)
    pages: list[dict] = []

    home = _Text()
    home.feed(_fetch(url))
    pages.append({"url": url, "title": home.title, "text": _clean(home.chunks)})

    # Same-host only. Following an outbound link would put someone else's copy
    # into this business's understanding of itself.
    seen = {url.rstrip("/")}
    for href in home.links:
        if len(pages) >= MAX_PAGES:
            break
        target = urljoin(url, href)
        parsed = urlparse(target)
        if parsed.hostname != origin.hostname:
            continue
        path = (parsed.path or "/").rstrip("/").lower()
        if not any(path.endswith(p) for p in WORTH_READING):
            continue
        clean_target = parsed._replace(query="", fragment="").geturl()
        if clean_target.rstrip("/") in seen:
            continue
        seen.add(clean_target.rstrip("/"))
        try:
            page = _Text()
            page.feed(_fetch(clean_target))
            pages.append({"url": clean_target, "title": page.title,
                          "text": _clean(page.chunks)})
        except (SiteError, requests.RequestException) as e:
            log.info("[site] skipped %s: %s", clean_target, e)

    combined = "\n\n".join(f"# {p['title'] or p['url']}\n{p['text']}" for p in pages)
    if len(combined.strip()) < 200:
        # A one-line splash page or a site that renders entirely in JavaScript.
        # Saying so lets the caller fall back to asking the owner directly,
        # rather than feeding the pipeline almost nothing and calling it
        # research.
        raise SiteError(
            "There was not enough text on that site to work from — "
            "it may be image-only or built in JavaScript."
        )

    return {
        "url": url,
        "title": home.title,
        "description": home.description,
        "pages": [p["url"] for p in pages],
        # Bounded because this becomes prompt input and the whole point is to
        # give the model the site, not to blow the context window on a blog.
        "text": combined[:20_000],
    }
