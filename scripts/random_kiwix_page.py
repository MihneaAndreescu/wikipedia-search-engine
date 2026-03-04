#!/usr/bin/env python3
"""Fetch a random Wikipedia article from local Kiwix and output plain text."""

from __future__ import annotations

import argparse
import html
import random
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "http://127.0.0.1:8080"
DEFAULT_USER_AGENT = "kiwix-random-page-script/2.0"
COMMON_LOCAL_PORTS = (8080, 8081, 8090, 8181, 8282, 9000, 3000, 5000)
COMMON_SEARCH_TOKENS = ("a", "e", "i", "o", "n", "s", "t", "r", "the", "of")
ASSET_SUFFIXES = (
    ".css",
    ".js",
    ".json",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".mp3",
    ".mp4",
    ".webm",
    ".pdf",
)


class PlainTextHTMLParser(HTMLParser):
    """Convert HTML into simple readable plain text."""

    _SKIP_TAGS = {"script", "style", "noscript", "svg", "math", "template"}
    _BREAK_TAGS = {
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "footer",
        "hr",
        "li",
        "main",
        "nav",
        "p",
        "section",
        "table",
        "tr",
        "ul",
        "ol",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self._BREAK_TAGS:
            self._push_break()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self._BREAK_TAGS:
            self._push_break()

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned:
            return
        if self._parts and not self._parts[-1].endswith(("\n", " ")):
            self._parts.append(" ")
        self._parts.append(cleaned)

    def get_text(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _push_break(self) -> None:
        if not self._parts:
            return
        if self._parts[-1].endswith("\n"):
            return
        self._parts.append("\n")


def normalize_base_url(base_url: str) -> str:
    value = base_url.strip()
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    if not value.endswith("/"):
        value += "/"
    return value


def get_host_port(base_url: str) -> tuple[str, int | None, str]:
    parsed = urlparse(base_url)
    host = parsed.hostname or ""
    scheme = parsed.scheme or "http"
    if parsed.port is not None:
        return host, parsed.port, scheme
    if scheme == "https":
        return host, 443, scheme
    return host, 80, scheme


def is_connection_error(exc: Exception) -> bool:
    if not isinstance(exc, URLError):
        return False
    text = str(exc).lower()
    return any(part in text for part in ("refused", "timed out", "failed to establish", "unreachable"))


def discover_local_base_url(
    base_url: str,
    timeout: float,
    attempts: list[tuple[str, Exception]],
) -> str | None:
    host, current_port, scheme = get_host_port(base_url)
    if host not in {"127.0.0.1", "localhost"}:
        return None

    for port in COMMON_LOCAL_PORTS:
        if port == current_port:
            continue
        candidate = f"{scheme}://{host}:{port}/"
        probe_urls = (
            urljoin(candidate, "catalog/v2/entries?count=1"),
            candidate,
        )
        for probe_url in probe_urls:
            result = try_fetch_html(probe_url, max(1.0, timeout / 3), attempts)
            if result is None:
                continue
            _, payload = result
            lowered = payload.lower()
            if "kiwix" in lowered or "/content/" in lowered or "catalog/v2" in lowered:
                return candidate
    return None


def fetch_html(url: str, timeout: float) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        final_url = response.geturl()
        charset = response.headers.get_content_charset() or "utf-8"
        html_text = response.read().decode(charset, errors="replace")
        return final_url, html_text


def try_fetch_html(url: str, timeout: float, attempts: list[tuple[str, Exception]]) -> tuple[str, str] | None:
    try:
        return fetch_html(url, timeout)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        attempts.append((url, exc))
        return None


def extract_content_names(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"/content/([^/?#\"'<>]+)",
        r"[?&]content=([^&\"'<>]+)",
    )
    for pattern in patterns:
        for raw in re.findall(pattern, text, flags=re.IGNORECASE):
            item = unquote(html.unescape(raw)).strip()
            if not item:
                continue
            if not re.fullmatch(r"[A-Za-z0-9._-]+", item):
                continue
            candidates.append(item)

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered_unique.append(candidate)
    return ordered_unique


def discover_content_names(base_url: str, timeout: float, attempts: list[tuple[str, Exception]]) -> list[str]:
    sources = (
        "catalog/v2/entries?count=200",
        "catalog/v2/entries",
        "catalog/v2/root.xml",
        "",
    )
    found: list[str] = []
    for path in sources:
        url = urljoin(base_url, path)
        result = try_fetch_html(url, timeout, attempts)
        if result is None:
            continue
        _, payload = result
        found.extend(extract_content_names(payload))

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            ordered_unique.append(item)
    return ordered_unique


def score_content(name: str, language: str | None) -> int:
    lower = name.lower()
    score = 0
    if "wikipedia" in lower:
        score += 200
    if "wiki" in lower:
        score += 20
    if language:
        token = f"_{language.lower()}_"
        if token in lower:
            score += 50
    if "_all_" in lower:
        score += 15
    if "maxi" in lower:
        score += 10
    return score


def pick_content_name(content_names: list[str], language: str | None) -> str | None:
    if not content_names:
        return None
    return max(content_names, key=lambda name: (score_content(name, language), -len(name)))


def extract_search_links(search_html: str, base_url: str, content_name: str) -> list[str]:
    marker = f"/content/{content_name}/"
    raw_links = re.findall(r'href=["\']([^"\']+)["\']', search_html, flags=re.IGNORECASE)
    links: list[str] = []
    for raw_link in raw_links:
        href = html.unescape(raw_link).strip()
        if not href or href.startswith("#"):
            continue
        if href.lower().startswith("javascript:"):
            continue
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        decoded_path = unquote(parsed.path)
        decoded_lower = decoded_path.lower()
        if marker not in decoded_path:
            continue
        if f"/content/{content_name}/-/" in decoded_path:
            continue
        if decoded_lower.endswith(ASSET_SUFFIXES):
            continue
        links.append(absolute)

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for link in links:
        if link not in seen:
            seen.add(link)
            ordered_unique.append(link)
    return ordered_unique


def get_random_page_by_content(
    base_url: str,
    content_name: str,
    timeout: float,
    attempts: list[tuple[str, Exception]],
) -> tuple[str, str] | None:
    random_url_candidates = (
        f"random?content={quote(content_name)}",
        f"Random?content={quote(content_name)}",
    )
    for candidate in random_url_candidates:
        result = try_fetch_html(urljoin(base_url, candidate), timeout, attempts)
        if result is not None:
            return result

    # Fallback: search broad tokens, pick a random article link from results.
    for token in COMMON_SEARCH_TOKENS:
        search_url = urljoin(
            base_url,
            f"search?content={quote(content_name)}&pattern={quote(token)}&pageLength=64",
        )
        search_result = try_fetch_html(search_url, timeout, attempts)
        if search_result is None:
            continue
        _, search_html = search_result
        article_links = extract_search_links(search_html, base_url, content_name)
        if not article_links:
            continue
        chosen = random.choice(article_links)
        page_result = try_fetch_html(chosen, timeout, attempts)
        if page_result is not None:
            return page_result
    return None


def get_random_page_html(
    base_url: str,
    random_path: str | None,
    content_name: str | None,
    language: str | None,
    timeout: float,
    allow_port_scan: bool = True,
) -> tuple[str, str]:
    attempts: list[tuple[str, Exception]] = []

    if random_path:
        direct = urljoin(base_url, random_path.lstrip("/"))
        direct_result = try_fetch_html(direct, timeout, attempts)
        if direct_result is not None:
            return direct_result
        raise RuntimeError(f"Could not fetch random page at configured path: {direct}")

    selected_content = content_name
    if not selected_content:
        discovered = discover_content_names(base_url, timeout, attempts)
        selected_content = pick_content_name(discovered, language)

    if selected_content:
        result = get_random_page_by_content(base_url, selected_content, timeout, attempts)
        if result is not None:
            return result

    # Final backward-compatible fallbacks for old/proxied setups.
    for path in ("random", "Random", "wiki/Special:Random"):
        result = try_fetch_html(urljoin(base_url, path), timeout, attempts)
        if result is not None:
            return result

    if attempts and all(is_connection_error(exc) for _, exc in attempts):
        if allow_port_scan:
            alternate_base = discover_local_base_url(base_url, timeout, attempts)
            if alternate_base:
                return get_random_page_html(
                    base_url=alternate_base,
                    random_path=random_path,
                    content_name=content_name,
                    language=language,
                    timeout=timeout,
                    allow_port_scan=False,
                )

        attempt_lines = []
        for url, exc in attempts[:16]:
            attempt_lines.append(f"- {url} ({exc})")
        details = "\n".join(attempt_lines) if attempt_lines else "- no attempts recorded"
        raise RuntimeError(
            "Could not connect to your Kiwix server. Ensure Kiwix is running and reachable."
            f"\nBase URL: {base_url}\nTried:\n{details}"
        )

    attempt_lines = []
    for url, exc in attempts[:12]:
        attempt_lines.append(f"- {url} ({exc})")
    details = "\n".join(attempt_lines) if attempt_lines else "- no attempts recorded"
    raise RuntimeError(
        "Could not fetch a random article from Kiwix."
        "\nIf needed, pass --content <zim_name> (example: wikipedia_en_all_maxi_2025-01)"
        "\nor pass --random-path /<zim-name>/random for custom routes."
        f"\nTried:\n{details}"
    )


def extract_title(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return "Unknown title"
    title = html.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*-\s*Wikipedia\s*$", "", title, flags=re.IGNORECASE)
    return title or "Unknown title"


def html_to_text(page_html: str) -> str:
    parser = PlainTextHTMLParser()
    parser.feed(page_html)
    parser.close()
    text = parser.get_text()
    return text or "(No readable text extracted.)"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch a random Wikipedia page from local Kiwix and convert it to plain text."
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Kiwix server URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--random-path",
        default=None,
        help="Explicit random endpoint path, e.g. /wikipedia_en_all_maxi_2025-01/random",
    )
    parser.add_argument(
        "--content",
        default=None,
        help="Explicit ZIM name, e.g. wikipedia_en_all_maxi_2025-01",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="Prefer a language code when auto-picking content (e.g. en, fr, ro).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output text file path.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Request timeout in seconds (default: 15).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    base_url = normalize_base_url(args.base_url)

    try:
        final_url, page_html = get_random_page_html(
            base_url=base_url,
            random_path=args.random_path,
            content_name=args.content,
            language=args.language,
            timeout=args.timeout,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    title = extract_title(page_html)
    body_text = html_to_text(page_html)
    output_text = f"Title: {title}\nURL: {final_url}\n\n{body_text}\n"

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_text, encoding="utf-8")
        print(f"Saved random page text to: {output_path.resolve()}")
        return 0

    print(output_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
