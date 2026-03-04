#!/usr/bin/env python3
"""Fetch a random Wikipedia article from local Kiwix as plain text."""

from __future__ import annotations

import argparse
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:8080/"
USER_AGENT = "kiwix-random-page-script/3.0"

class TextExtractor(HTMLParser):
    """Convert HTML to readable plain text without dependencies."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "math"}
    BREAK_TAGS = {
        "article",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in self.BREAK_TAGS:
            self._push_newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in self.BREAK_TAGS:
            self._push_newline()

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self.parts and not self.parts[-1].endswith(("\n", " ")):
            self.parts.append(" ")
        self.parts.append(text)

    def text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() or "(No readable text extracted.)"

    def _push_newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

def normalize_base_url(base_url: str) -> str:
    url = base_url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"http://{url}"
    if not url.endswith("/"):
        url += "/"
    return url

def fetch_text(url: str, timeout: float) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
        candidates = ["utf-8", response.headers.get_content_charset(), "cp1252", "latin-1"]
        body = ""
        for encoding in candidates:
            if not encoding:
                continue
            try:
                body = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if not body:
            body = raw.decode("utf-8", errors="replace")
        return response.geturl(), body

def discover_content_name(base_url: str, timeout: float) -> str:
    pattern = r'href=["\']/?content/([^/"\'?#]+)'
    sources = ("catalog/v2/entries?count=100", "")
    names: list[str] = []

    for path in sources:
        url = urljoin(base_url, path)
        try:
            _, body = fetch_text(url, timeout)
        except (HTTPError, URLError, TimeoutError):
            continue
        names.extend(re.findall(pattern, body, flags=re.IGNORECASE))

    unique_names: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)

    if not unique_names:
        raise RuntimeError(
            "Could not auto-detect a Kiwix content name. "
            "Pass --content (example: wikipedia_en_all_maxi_2025-08)."
        )

    wikipedia_names = [name for name in unique_names if "wikipedia" in name.lower()]
    return wikipedia_names[0] if wikipedia_names else unique_names[0]

def extract_title(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return "Unknown title"
    title = html.unescape(match.group(1))
    title = re.sub(r"\s+", " ", title).strip()
    title = re.sub(r"\s*-\s*Wikipedia\s*$", "", title, flags=re.IGNORECASE)
    return title or "Unknown title"

def html_to_text(page_html: str) -> str:
    parser = TextExtractor()
    parser.feed(page_html)
    parser.close()
    return parser.text()

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch one random page from local Kiwix and output plain text."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Kiwix URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--content", default=None, help="Optional content name, e.g. wikipedia_en_all_maxi_2025-08")
    parser.add_argument("--output", default=None, help="Optional output text file path")
    parser.add_argument("--timeout", type=float, default=15.0, help="Request timeout in seconds (default: 15)")
    return parser.parse_args(argv)

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    base_url = normalize_base_url(args.base_url)

    try:
        content_name = args.content or discover_content_name(base_url, args.timeout)
        random_url = urljoin(base_url, f"random?content={quote(content_name)}")
        final_url, page_html = fetch_text(random_url, args.timeout)
    except URLError as exc:
        print(
            f"Error: Cannot reach Kiwix at {base_url}. Start kiwix-serve first. ({exc})",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output_text = f"Title: {extract_title(page_html)}\nURL: {final_url}\n\n{html_to_text(page_html)}\n"

    if args.output:
        output_path = Path(args.output)
        output_path.write_text(output_text, encoding="utf-8")
        print(f"Saved random page text to: {output_path.resolve()}")
        return 0

    print(output_text)
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
