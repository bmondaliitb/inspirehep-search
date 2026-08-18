#!/usr/bin/env python3
"""List active INSPIRE HEP jobs grouped by experiment.

The script uses INSPIRE's public JSON API rather than scraping rendered HTML.
It has no third-party dependencies and defaults to active hep-ex positions.
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import sys
import time
import textwrap
from collections import defaultdict
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://inspirehep.net/api/jobs"
USER_AGENT = "inspire-jobs-by-experiment/1.0 (+https://inspirehep.net)"
UNTAGGED = "Unspecified experiment"


class _HTMLTextExtractor(HTMLParser):
    """Turn the small amount of HTML in job descriptions into plain text."""

    _BLOCK_TAGS = {"br", "div", "li", "p", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def text(self) -> str:
        lines = (" ".join(line.split()) for line in "".join(self.parts).splitlines())
        return "\n".join(line for line in lines if line)


def html_to_text(value: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return html.unescape(parser.text()).strip()


def _request_json(url: str, timeout: float, retries: int = 3) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("INSPIRE returned an unexpected JSON response")
            return payload
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise RuntimeError(f"INSPIRE API request failed with HTTP {exc.code}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Could not read the INSPIRE API: {exc}") from exc
            delay = 2**attempt
        time.sleep(delay)
    raise AssertionError("unreachable")


def subtract_months(day: date, months: int) -> date:
    """Subtract calendar months, clamping the day at the end of the month."""
    month_index = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _parse_deadline(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def fetch_jobs(category: str, window: str, timeout: float) -> list[dict[str, Any]]:
    """Fetch jobs in *category* for the requested deadline window."""
    if window == "current":
        query = f"status:open AND arxiv_categories:{category}"
    else:
        cutoff = subtract_months(date.today(), int(window.removesuffix("m"))).isoformat()
        query = f"(status:open OR deadline_date:[{cutoff} TO *]) AND arxiv_categories:{category}"
    page_size = 100
    url: str | None = f"{API_URL}?{urlencode({'q': query, 'sort': 'deadline', 'size': page_size})}"
    jobs: list[dict[str, Any]] = []

    while url:
        payload = _request_json(url, timeout)
        hits = payload.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            raise RuntimeError("INSPIRE returned an unexpected jobs response")
        jobs.extend(hit for hit in hits if isinstance(hit, dict))
        next_url = payload.get("links", {}).get("next")
        url = next_url if isinstance(next_url, str) else None

    return jobs


def _values(items: Any, key: str) -> list[str]:
    if not isinstance(items, list):
        return []
    return [item[key].strip() for item in items if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()]


def normalize_job(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    job_id = str(hit.get("id") or metadata.get("control_number") or "")
    experiments = sorted(set(_values(metadata.get("accelerator_experiments"), "name")), key=str.casefold)
    external_urls = _values(metadata.get("urls"), "value")
    deadline = metadata.get("deadline_date")
    created = hit.get("created") or metadata.get("legacy_creation_date")

    return {
        "id": job_id,
        "status": str(metadata.get("status") or "").lower(),
        "title": metadata.get("position") or "Untitled position",
        "institutions": _values(metadata.get("institutions"), "value"),
        "ranks": [str(rank) for rank in metadata.get("ranks", []) if rank],
        "regions": [str(region) for region in metadata.get("regions", []) if region],
        "deadline": str(deadline) if deadline else None,
        "created": str(created) if created else None,
        "experiments": experiments,
        "description": html_to_text(str(metadata.get("description") or "")),
        "inspire_url": f"https://inspirehep.net/jobs/{job_id}" if job_id else "https://inspirehep.net/jobs",
        "application_urls": external_urls,
    }


def deadline_key(job: dict[str, Any]) -> tuple[date, str]:
    value = job.get("deadline")
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        parsed = date.max
    return parsed, str(job.get("title", "")).casefold()


def group_jobs(jobs: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        experiments = job["experiments"] or [UNTAGGED]
        for experiment in experiments:
            grouped[experiment].append(job)
    for advertisements in grouped.values():
        advertisements.sort(key=deadline_key)
    return dict(grouped)


def _display_name(value: str) -> str:
    rank_names = {
        "JUNIOR": "Junior",
        "MASTER": "Master's",
        "OTHER": "Other",
        "PHD": "PhD",
        "POSTDOC": "Postdoc",
        "SENIOR": "Senior",
        "STAFF": "Staff",
        "UNDERGRADUATE": "Undergraduate",
    }
    return rank_names.get(value.upper(), value.replace("_", " ").title())


def _ordered_groups(groups: dict[str, list[dict[str, Any]]]) -> list[tuple[str, list[dict[str, Any]]]]:
    return sorted(
        groups.items(),
        key=lambda item: (item[0] == UNTAGGED, -len(item[1]), item[0].casefold()),
    )


def render_text(jobs: list[dict[str, Any]], category: str, details: bool, width: int) -> str:
    groups = group_jobs(jobs)
    tagged_jobs = sum(bool(job["experiments"]) for job in jobs)
    lines = [
        f"Active {category} advertisements: {len(jobs)}",
        f"With explicit experiment tags: {tagged_jobs}",
        "",
        "EXPERIMENTS HIRING",
    ]
    for experiment, advertisements in _ordered_groups(groups):
        lines.append(f"  {experiment}: {len(advertisements)}")

    lines.extend(("", "ADVERTISEMENTS BY EXPERIMENT"))
    for experiment, advertisements in _ordered_groups(groups):
        lines.extend(("", f"== {experiment} ({len(advertisements)}) =="))
        for index, job in enumerate(advertisements, 1):
            institutions = ", ".join(job["institutions"]) or "Institution not specified"
            facts = []
            if job["ranks"]:
                facts.append(" / ".join(_display_name(rank) for rank in job["ranks"]))
            if job["regions"]:
                facts.append(", ".join(job["regions"]))
            facts.append(f"deadline {job['deadline'] or 'not specified'}")
            lines.append(f"\n{index}. {job['title']} — {institutions}")
            lines.append(f"   {' | '.join(facts)}")
            lines.append(f"   INSPIRE: {job['inspire_url']}")
            for apply_url in job["application_urls"]:
                lines.append(f"   Apply:   {apply_url}")
            if details and job["description"]:
                wrapped = textwrap.fill(
                    job["description"].replace("\n", " "),
                    width=width,
                    initial_indent="   ",
                    subsequent_indent="   ",
                )
                lines.extend(("   Description:", wrapped))
    return "\n".join(lines).rstrip() + "\n"


def render_json(jobs: list[dict[str, Any]], category: str, window: str, ranks: list[str]) -> str:
    groups = group_jobs(jobs)
    output = {
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": category,
        "window": window,
        "ranks": ranks,
        "unique_advertisement_count": len(jobs),
        "groups": [
            {"experiment": experiment, "count": len(ads), "advertisements": ads}
            for experiment, ads in _ordered_groups(groups)
        ],
    }
    return json.dumps(output, indent=2, ensure_ascii=False) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch active INSPIRE HEP jobs and group their advertisements by experiment."
    )
    parser.add_argument(
        "--category",
        default="hep-ex",
        help="INSPIRE arXiv category to fetch (default: hep-ex)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="maximum number of advertisements to fetch; 0 means all (default: 0)",
    )
    parser.add_argument(
        "--window",
        choices=("current", "3m", "6m"),
        default="current",
        help="current openings, or current plus ads with deadlines in the last 3/6 months (default: current)",
    )
    parser.add_argument(
        "--rank",
        nargs="+",
        choices=("postdoc", "senior"),
        metavar="{postdoc,senior}",
        help="only show one or both position ranks (default: all ranks)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="include the full plain-text job description in text output",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON, including descriptions")
    parser.add_argument("--output", type=Path, help="write results to this file instead of stdout")
    parser.add_argument("--timeout", type=float, default=30.0, help="request timeout in seconds (default: 30)")
    parser.add_argument("--width", type=int, default=100, help="description wrap width (default: 100)")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be zero or greater")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    if args.width < 40:
        parser.error("--width must be at least 40")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        raw_jobs = fetch_jobs(args.category, args.window, args.timeout)
        jobs = [normalize_job(job) for job in raw_jobs]
        if args.window == "current":
            jobs = [job for job in jobs if job["status"] == "open"]
        else:
            cutoff = subtract_months(date.today(), int(args.window.removesuffix("m")))
            jobs = [
                job
                for job in jobs
                if job["status"] == "open" or (_parse_deadline(job["deadline"]) or date.min) >= cutoff
            ]
        if args.rank:
            requested_ranks = {rank.upper() for rank in args.rank}
            jobs = [job for job in jobs if requested_ranks.intersection(job["ranks"])]
        if args.limit:
            jobs = jobs[: args.limit]
        output = (
            render_json(jobs, args.category, args.window, args.rank or [])
            if args.json
            else render_text(jobs, args.category, args.details, args.width)
        )
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
