#!/usr/bin/env python3
"""Create a static export of a GitHub repository's conversation history.

This captures everything that would *not* be part of a normal code fork, namely
issues, pull requests, and discussions (including their comments and any
embedded images), and writes them to disk as JSON plus human-readable Markdown.

Motivation: keep a durable, offline copy of the extensive conversation history
in the AccelerationConsortium/ac-training-lab repository. See
https://github.com/AccelerationConsortium/ac-training-lab issue #1.

The export is intentionally "static":

* Embedded images / attachments referenced in issue, PR, and discussion bodies
  and comments are downloaded and the Markdown is rewritten to point at the
  local copies.
* ``@mentions`` are neutralized (a zero-width space is inserted after the ``@``)
  so that re-posting or viewing the export never notifies / tags anyone.

Only the Python standard library is used so the script can be run without
installing any additional dependencies.

Usage::

    export GITHUB_TOKEN=ghp_...   # a token with read access to the repo
    python scripts/export_github_conversations.py \
        --repo AccelerationConsortium/ac-training-lab \
        --output github_export

A token is optional for public repositories but is strongly recommended to
avoid the very low unauthenticated rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha1
from pathlib import Path
from typing import Dict, Iterable, List, Optional

REST_API = "https://api.github.com"
GRAPHQL_API = "https://api.github.com/graphql"
USER_AGENT = "ac-training-lab-conversation-exporter"

# Matches Markdown image/attachment URLs, both ``![alt](url)`` and bare
# ``<img src="url">`` as well as GitHub user-attachment links.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(\s*(<[^>]+>|[^)\s]+)")
_HTML_IMG_RE = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

# Matches an ``@mention`` that is not an email address. The leading character
# must be a boundary that GitHub itself treats as the start of a mention.
_MENTION_RE = re.compile(r"(^|[^\w@/])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))")

ZERO_WIDTH_SPACE = "\u200b"


def neutralize_mentions(text: Optional[str]) -> str:
    """Insert a zero-width space after ``@`` so mentions never notify anyone.

    ``@octocat`` becomes ``@\u200boctocat`` which renders identically but is not
    treated as a mention by GitHub.
    """

    if not text:
        return text or ""

    def _replace(match: "re.Match") -> str:
        return f"{match.group(1)}@{ZERO_WIDTH_SPACE}{match.group(2)}"

    return _MENTION_RE.sub(_replace, text)


def extract_asset_urls(text: Optional[str]) -> List[str]:
    """Return the list of embedded image / attachment URLs found in ``text``."""

    if not text:
        return []
    urls: List[str] = []
    for match in _MD_IMAGE_RE.finditer(text):
        url = match.group(1).strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        # A Markdown image target may contain an optional title after the URL.
        url = url.split()[0] if url else url
        if url:
            urls.append(url)
    for match in _HTML_IMG_RE.finditer(text):
        urls.append(match.group(1).strip())
    # Preserve order but drop duplicates.
    seen = set()
    unique: List[str] = []
    for url in urls:
        if url not in seen and _is_http_url(url):
            seen.add(url)
            unique.append(url)
    return unique


def _is_http_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def asset_filename(url: str) -> str:
    """Return a stable, filesystem-safe filename for an asset URL."""

    parsed = urllib.parse.urlparse(url)
    suffix = Path(parsed.path).suffix
    if len(suffix) > 10 or "/" in suffix:
        suffix = ""
    digest = sha1(url.encode("utf-8")).hexdigest()[:16]
    return f"{digest}{suffix}"


def localize_assets(text: Optional[str], url_to_local: Dict[str, str]) -> str:
    """Rewrite asset URLs in ``text`` to their local (relative) paths."""

    if not text:
        return text or ""
    result = text
    for url, local in url_to_local.items():
        result = result.replace(f"<{url}>", local)
        result = result.replace(url, local)
    return result


class GitHubClient:
    """Minimal GitHub REST + GraphQL client built on the standard library."""

    def __init__(self, token: Optional[str] = None):
        self.token = token

    def _headers(self, accept: str) -> Dict[str, str]:
        headers = {"Accept": accept, "User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = "token " + self.token
        return headers

    def _request(self, request: urllib.request.Request, retries: int = 3):
        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(request) as response:
                    return response.read(), dict(response.headers)
            except urllib.error.HTTPError as error:
                # Respect secondary rate limits / transient server errors.
                if error.code in (403, 429, 500, 502, 503) and attempt < retries - 1:
                    reset = error.headers.get("Retry-After")
                    delay = float(reset) if reset else 2 ** attempt
                    time.sleep(delay)
                    last_error = error
                    continue
                raise
            except urllib.error.URLError as error:
                last_error = error
                time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    def rest(self, path: str, params: Optional[Dict[str, str]] = None) -> List[dict]:
        """Return all pages of a paginated REST endpoint as a flat list."""

        query = dict(params or {})
        query.setdefault("per_page", "100")
        url = f"{REST_API}{path}?{urllib.parse.urlencode(query)}"
        results: List[dict] = []
        while url:
            request = urllib.request.Request(
                url, headers=self._headers("application/vnd.github+json")
            )
            body, headers = self._request(request)
            page = json.loads(body)
            if isinstance(page, list):
                results.extend(page)
            else:
                results.append(page)
            url = _next_link(headers.get("Link", ""))
        return results

    def graphql(self, query: str, variables: Dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            GRAPHQL_API,
            data=payload,
            headers=self._headers("application/json"),
            method="POST",
        )
        body, _ = self._request(request)
        data = json.loads(body)
        if "errors" in data:
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    def download(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers=self._headers("*/*"))
        body, _ = self._request(request)
        return body


def _next_link(link_header: str) -> Optional[str]:
    """Parse the ``Link`` header and return the ``rel="next"`` URL, if any."""

    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url = section[0].strip().lstrip("<").rstrip(">")
        for rel in section[1:]:
            if rel.strip() == 'rel="next"':
                return url
    return None


DISCUSSIONS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    discussions(first: 50, after: $cursor,
                orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        url
        createdAt
        updatedAt
        category { name }
        author { login }
        comments(first: 100) {
          nodes {
            body
            createdAt
            author { login }
            replies(first: 100) {
              nodes { body createdAt author { login } }
            }
          }
        }
      }
    }
  }
}
"""


def fetch_discussions(client: GitHubClient, owner: str, name: str) -> List[dict]:
    discussions: List[dict] = []
    cursor: Optional[str] = None
    while True:
        data = client.graphql(
            DISCUSSIONS_QUERY, {"owner": owner, "name": name, "cursor": cursor}
        )
        repo = data.get("repository") or {}
        block = repo.get("discussions") or {}
        discussions.extend(block.get("nodes") or [])
        page_info = block.get("pageInfo") or {}
        if page_info.get("hasNextPage"):
            cursor = page_info.get("endCursor")
        else:
            break
    return discussions


def _author_login(item: dict) -> str:
    author = item.get("author") or item.get("user") or {}
    if isinstance(author, dict):
        return author.get("login") or "unknown"
    return "unknown"


def _collect_and_localize(
    client: GitHubClient, text: Optional[str], assets_dir: Path, download: bool
) -> str:
    """Neutralize mentions, download embedded assets, and relocalize ``text``."""

    text = text or ""
    url_to_local: Dict[str, str] = {}
    for url in extract_asset_urls(text):
        filename = asset_filename(url)
        local_rel = f"assets/{filename}"
        if download:
            dest = assets_dir / filename
            if not dest.exists():
                try:
                    dest.write_bytes(client.download(url))
                except (urllib.error.URLError, urllib.error.HTTPError) as error:
                    print(f"  ! could not download {url}: {error}", file=sys.stderr)
                    continue
        url_to_local[url] = local_rel
    text = localize_assets(text, url_to_local)
    return neutralize_mentions(text)


def _render_thread(title: str, meta: List[str], sections: Iterable[str]) -> str:
    lines = [f"# {title}", ""]
    lines.extend(meta)
    lines.append("")
    lines.extend(sections)
    return "\n".join(lines).rstrip() + "\n"


def export_issue_like(
    client: GitHubClient,
    items: List[dict],
    kind: str,
    out_dir: Path,
    assets_dir: Path,
    download: bool,
) -> int:
    count = 0
    for item in items:
        number = item["number"]
        slug = f"{kind}-{number:04d}"
        body = _collect_and_localize(client, item.get("body"), assets_dir, download)
        comments = client.rest(
            f"/repos/{item['_owner']}/{item['_name']}/issues/{number}/comments"
        )
        sections = [f"## Original post by @{_author_login(item)}", "", body, ""]
        for comment in comments:
            c_body = _collect_and_localize(
                client, comment.get("body"), assets_dir, download
            )
            sections.append(
                f"## Comment by @{neutralize_mentions(_author_login(comment))}"
            )
            sections.append("")
            sections.append(c_body)
            sections.append("")

        meta = [
            f"- **Type:** {kind}",
            f"- **Number:** {number}",
            f"- **State:** {item.get('state', 'unknown')}",
            f"- **Created:** {item.get('created_at', 'unknown')}",
            f"- **URL:** {item.get('html_url', '')}",
        ]
        markdown = _render_thread(
            neutralize_mentions(item.get("title", slug)), meta, sections
        )
        (out_dir / f"{slug}.md").write_text(markdown, encoding="utf-8")
        (out_dir / f"{slug}.json").write_text(
            json.dumps({"item": item, "comments": comments}, indent=2, default=str),
            encoding="utf-8",
        )
        count += 1
        print(f"  exported {slug}")
    return count


def export_discussions(
    client: GitHubClient,
    discussions: List[dict],
    out_dir: Path,
    assets_dir: Path,
    download: bool,
) -> int:
    count = 0
    for disc in discussions:
        number = disc["number"]
        slug = f"discussion-{number:04d}"
        body = _collect_and_localize(client, disc.get("body"), assets_dir, download)
        sections = [f"## Original post by @{_author_login(disc)}", "", body, ""]
        for comment in (disc.get("comments") or {}).get("nodes") or []:
            c_body = _collect_and_localize(
                client, comment.get("body"), assets_dir, download
            )
            sections.append(f"## Comment by @{_author_login(comment)}")
            sections.append("")
            sections.append(c_body)
            sections.append("")
            for reply in (comment.get("replies") or {}).get("nodes") or []:
                r_body = _collect_and_localize(
                    client, reply.get("body"), assets_dir, download
                )
                sections.append(f"### Reply by @{_author_login(reply)}")
                sections.append("")
                sections.append(r_body)
                sections.append("")

        meta = [
            "- **Type:** discussion",
            f"- **Number:** {number}",
            f"- **Category:** {(disc.get('category') or {}).get('name', 'unknown')}",
            f"- **Created:** {disc.get('createdAt', 'unknown')}",
            f"- **URL:** {disc.get('url', '')}",
        ]
        markdown = _render_thread(
            neutralize_mentions(disc.get("title", slug)), meta, sections
        )
        (out_dir / f"{slug}.md").write_text(markdown, encoding="utf-8")
        (out_dir / f"{slug}.json").write_text(
            json.dumps(disc, indent=2, default=str), encoding="utf-8"
        )
        count += 1
        print(f"  exported {slug}")
    return count


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default="AccelerationConsortium/ac-training-lab",
        help="Repository to export, in 'owner/name' form.",
    )
    parser.add_argument(
        "--output",
        default="github_export",
        type=Path,
        help="Directory to write the export into.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("GITHUB_TOKEN"),
        help="GitHub token (defaults to the GITHUB_TOKEN environment variable).",
    )
    parser.add_argument(
        "--no-assets",
        action="store_true",
        help="Skip downloading embedded images/attachments.",
    )
    parser.add_argument(
        "--skip-discussions",
        action="store_true",
        help="Skip discussions (which require the GraphQL API).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if "/" not in args.repo:
        print("--repo must be in 'owner/name' form", file=sys.stderr)
        return 2
    owner, name = args.repo.split("/", 1)
    download = not args.no_assets

    output: Path = args.output
    assets_dir = output / "assets"
    issues_dir = output / "issues"
    prs_dir = output / "pull_requests"
    discussions_dir = output / "discussions"
    for directory in (assets_dir, issues_dir, prs_dir, discussions_dir):
        directory.mkdir(parents=True, exist_ok=True)

    client = GitHubClient(args.token)

    print(f"Fetching issues and pull requests for {args.repo} ...")
    raw = client.rest(f"/repos/{owner}/{name}/issues", {"state": "all"})
    for item in raw:
        item["_owner"], item["_name"] = owner, name
    issues = [item for item in raw if "pull_request" not in item]
    pulls = [item for item in raw if "pull_request" in item]

    n_issues = export_issue_like(
        client, issues, "issue", issues_dir, assets_dir, download
    )
    n_pulls = export_issue_like(client, pulls, "pr", prs_dir, assets_dir, download)

    n_discussions = 0
    if not args.skip_discussions:
        print("Fetching discussions ...")
        try:
            discussions = fetch_discussions(client, owner, name)
            n_discussions = export_discussions(
                client, discussions, discussions_dir, assets_dir, download
            )
        except (RuntimeError, urllib.error.HTTPError) as error:
            print(f"  ! could not export discussions: {error}", file=sys.stderr)

    summary = {
        "repository": args.repo,
        "issues": n_issues,
        "pull_requests": n_pulls,
        "discussions": n_discussions,
        "assets_downloaded": download,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(
        f"Done: {n_issues} issues, {n_pulls} PRs, {n_discussions} discussions "
        f"written to {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
