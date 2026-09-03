#!/usr/bin/env python3
"""Collect physics jobs from INSPIRE and major academic job boards."""

from __future__ import annotations

import argparse
import calendar
import html
import json
import re
import sys
import time
import textwrap
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

API_URL = "https://inspirehep.net/api/jobs"
USER_AGENT = "physics-job-collector/2.0 (+https://inspirehep.net)"
UNTAGGED = "Unspecified experiment"
MAX_PAGES = 50
SOURCE_LABELS = {
    "inspire": "INSPIRE", "physicsworld": "Physics World Jobs",
    "academicjobs": "AcademicJobs.com", "euraxess": "EURAXESS",
    "academicpositions": "Academic Positions", "jobsacuk": "jobs.ac.uk",
    "academicjobsonline": "AcademicJobsOnline", "aps": "APS Physics Jobs",
    "aas": "AAS Job Register", "nature": "Nature Careers",
    "science": "Science Careers", "findapostdoc": "FindAPostDoc",
    "higheredjobs": "HigherEdJobs", "cern": "CERN Careers",
}
ALL_SOURCES = tuple(SOURCE_LABELS)


class PartialSourceError(RuntimeError):
    """A later result page failed after earlier pages were parsed."""

    def __init__(self, message: str, jobs: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.jobs = jobs


class _HTMLTextExtractor(HTMLParser):
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


def _request_bytes(url: str, timeout: float, retries: int = 2,
                   accept: str = "text/html,application/xhtml+xml") -> bytes:
    request = Request(url, headers={"Accept": accept, "Accept-Language": "en-US,en;q=0.8", "User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable or attempt == retries:
                raise RuntimeError(f"HTTP {exc.code} while reading {url}") from exc
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
        except (URLError, TimeoutError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Could not read {url}: {exc}") from exc
            delay = 2**attempt
        time.sleep(delay)
    raise AssertionError("unreachable")


def _request_text(url: str, timeout: float) -> str:
    return _request_bytes(url, timeout).decode("utf-8", errors="replace")


def _request_json(url: str, timeout: float, retries: int = 3) -> dict[str, Any]:
    try:
        result = json.loads(_request_bytes(url, timeout, retries, "application/json").decode())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected JSON response from {url}") from exc
    if not isinstance(result, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return result


def subtract_months(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _parse_deadline(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _parse_human_date(value: str, *, year_if_missing: bool = False) -> str | None:
    cleaned = " ".join(html_to_text(value).replace(",", " ").split())
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d %Y", "%B %d %Y"):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            pass
    if year_if_missing:
        for fmt in ("%d %b", "%d %B"):
            try:
                parsed = datetime.strptime(f"{cleaned} {date.today().year}", f"{fmt} %Y").date()
                if parsed < date.today() - timedelta(days=180):
                    parsed = parsed.replace(year=parsed.year + 1)
                return parsed.isoformat()
            except ValueError:
                pass
    return None


def _values(items: Any, key: str) -> list[str]:
    if not isinstance(items, list):
        return []
    return [item[key].strip() for item in items if isinstance(item, dict) and isinstance(item.get(key), str) and item[key].strip()]


def _clean_url(value: str, base: str) -> str:
    parts = urlsplit(urljoin(base, html.unescape(value)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _attr(tag: str, name: str) -> str | None:
    match = re.search(rf"\b{re.escape(name)}\s*=\s*([\"'])(.*?)\1", tag, re.I | re.S)
    return html.unescape(match.group(2)) if match else None


def _first(pattern: str, value: str, flags: int = re.I | re.S) -> str:
    match = re.search(pattern, value, flags)
    return html_to_text(match.group(1)) if match else ""


def _next_url(document: str, base: str) -> str | None:
    for tag in re.findall(r"<(?:a|link)\b[^>]*>", document, re.I):
        rel, aria = (_attr(tag, "rel") or "").lower().split(), (_attr(tag, "aria-label") or "").lower()
        if "next" in rel or "next page" in aria or "next page" in aria:
            if href := _attr(tag, "href"):
                return urljoin(base, href)
    return None


def _infer_ranks(text: str) -> list[str]:
    value, ranks = text.casefold(), []
    if re.search(r"\b(ph\.?d|doctoral|studentship|graduate student)\b", value):
        ranks.append("PHD")
    if re.search(r"\b(post[ -]?doc(?:toral)?|research associate|research fellow|fellowship)\b", value):
        ranks.append("POSTDOC")
    if re.search(r"\b(professor|faculty|lecturer|senior|director|group leader|staff scientist)\b", value):
        ranks.append("SENIOR")
    return ranks


def _new_job(source: str, job_id: str, title: str, url: str, *, institutions: Iterable[str] = (),
             regions: Iterable[str] = (), deadline: str | None = None, created: str | None = None,
             description: str = "", ranks: Iterable[str] = (), application_urls: Iterable[str] = (),
             experiments: Iterable[str] = ()) -> dict[str, Any]:
    deadline_day = _parse_deadline(deadline)
    label = SOURCE_LABELS[source]
    return {
        "id": f"{source}:{job_id}", "status": "closed" if deadline_day and deadline_day < date.today() else "open",
        "title": " ".join(title.split()) or "Untitled position",
        "institutions": [x for x in dict.fromkeys(institutions) if x],
        "ranks": sorted(set(ranks) | set(_infer_ranks(title))),
        "regions": [x for x in dict.fromkeys(regions) if x], "deadline": deadline, "created": created,
        "experiments": sorted(set(experiments), key=str.casefold), "description": description.strip(),
        "source": label, "source_url": url, "sources": [label], "source_urls": [url],
        "application_urls": [x for x in dict.fromkeys(application_urls) if x and x != url],
    }


CATEGORY_TERMS = {
    "hep-ex": ("particle physics", "high energy physics", "high-energy physics", "experimental physics",
               "experimental particle", "accelerator physic", "collider", "detector physic", "astroparticle",
               "neutrino", "dark matter", "axion", "hadron", "atlas", "cms", "lhcb", "belle ii",
               "dune", "muon", "cosmic ray"),
    "hep-th": ("theoretical physics", "high energy theory", "quantum gravity", "string theory", "field theory"),
    "astro-ph": ("astronomy", "astrophysics", "cosmology", "gravitational wave", "space science"),
    "nucl-ex": ("nuclear physics", "nuclear experiment", "heavy ion", "hadron"),
    "nucl-th": ("nuclear theory", "theoretical nuclear", "many-body"),
}


def _relevant(text: str, category: str) -> bool:
    folded = unicodedata.normalize("NFKD", html_to_text(text)).casefold()
    if terms := CATEGORY_TERMS.get(category):
        def contains(term: str) -> bool:
            # A few entries are intentional word stems; short experiment and
            # laboratory names must be whole words ("DESY" is in "geodesy").
            if term.endswith((" physic", "hadron", "muon")):
                return term in folded
            return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", folded))
        return any(contains(term) for term in terms)
    words = [x for x in re.split(r"[-_. ]+", category.casefold()) if len(x) > 2]
    return bool(words) and all(x in folded for x in words)


def fetch_jobs(category: str, window: str, timeout: float) -> list[dict[str, Any]]:
    """Fetch raw INSPIRE API hits (retained for API compatibility)."""
    query = (f"status:open AND arxiv_categories:{category}" if window == "current" else
             f"(status:open OR deadline_date:[{subtract_months(date.today(), int(window[:-1])).isoformat()} TO *]) AND arxiv_categories:{category}")
    url: str | None = f"{API_URL}?{urlencode({'q': query, 'sort': 'deadline', 'size': 100})}"
    jobs: list[dict[str, Any]] = []
    while url:
        payload = _request_json(url, timeout)
        hits = payload.get("hits", {}).get("hits", [])
        if not isinstance(hits, list):
            raise RuntimeError("INSPIRE returned an unexpected jobs response")
        jobs.extend(x for x in hits if isinstance(x, dict))
        next_url = payload.get("links", {}).get("next")
        url = next_url if isinstance(next_url, str) else None
    return jobs


def normalize_job(hit: dict[str, Any]) -> dict[str, Any]:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    job_id = str(hit.get("id") or metadata.get("control_number") or "")
    url = f"https://inspirehep.net/jobs/{job_id}" if job_id else "https://inspirehep.net/jobs"
    job = _new_job("inspire", job_id, str(metadata.get("position") or "Untitled position"), url,
                   institutions=_values(metadata.get("institutions"), "value"),
                   ranks=[str(x) for x in metadata.get("ranks", []) if x],
                   regions=[str(x) for x in metadata.get("regions", []) if x],
                   deadline=str(metadata.get("deadline_date")) if metadata.get("deadline_date") else None,
                   created=str(hit.get("created") or metadata.get("legacy_creation_date") or "") or None,
                   experiments=_values(metadata.get("accelerator_experiments"), "name"),
                   description=html_to_text(str(metadata.get("description") or "")),
                   application_urls=_values(metadata.get("urls"), "value"))
    job["status"], job["inspire_url"] = str(metadata.get("status") or job["status"]).lower(), url
    return job


def _fetch_paginated(start: str, timeout: float,
                     parser: Callable[[str, str], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    jobs, seen, url = [], set(), start
    while url and url not in seen and len(seen) < MAX_PAGES:
        seen.add(url)
        try:
            document = _request_text(url, timeout)
        except RuntimeError as exc:
            if jobs:
                raise PartialSourceError(str(exc), jobs) from exc
            raise
        jobs.extend(parser(document, url))
        url = _next_url(document, url)
    return jobs


def _parse_madgex(document: str, base: str, source: str) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for block in re.split(r"(?=<li\s+class=[\"'][^\"']*lister__item)", document, flags=re.I)[1:]:
        header = re.search(r"<h3\b[^>]*lister__header[^>]*>(.*?)</h3>", block, re.I | re.S)
        anchor = re.search(r"<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", header.group(1), re.I | re.S) if header else None
        if not anchor:
            continue
        url, title = _clean_url(anchor.group(1), base), html_to_text(anchor.group(2))
        job_id = _first(r"\bid=[\"']item-([^\"']+)", block) or url.rstrip("/").split("/")[-2]
        def meta(kind: str) -> str:
            return _first(rf"<li\b[^>]*lister__meta-item--{kind}[^>]*>(.*?)</li>", block)
        description, salary = _first(r"<p\b[^>]*lister__description[^>]*>(.*?)</p>", block), meta("salary")
        jobs.append(_new_job(source, job_id, title, url, institutions=[meta("recruiter")], regions=[meta("location")],
                             description=" — ".join(x for x in (description, salary) if x)))
    return jobs


def _madgex(source: str, start: str, category: str, timeout: float, prefiltered: bool = False) -> list[dict[str, Any]]:
    try:
        jobs = _fetch_paginated(start, timeout, lambda doc, base: _parse_madgex(doc, base, source))
    except PartialSourceError as exc:
        exc.jobs = exc.jobs if prefiltered else [x for x in exc.jobs if _relevant(_job_text(x), category)]
        raise
    return jobs if prefiltered else [x for x in jobs if _relevant(_job_text(x), category)]


def _relevant_paginated(start: str, timeout: float, parser: Callable[[str, str], list[dict[str, Any]]],
                        category: str) -> list[dict[str, Any]]:
    try:
        return [x for x in _fetch_paginated(start, timeout, parser) if _relevant(_job_text(x), category)]
    except PartialSourceError as exc:
        exc.jobs = [x for x in exc.jobs if _relevant(_job_text(x), category)]
        raise


def _parse_academic_positions(document: str, base: str) -> list[dict[str, Any]]:
    jobs = []
    for block in re.split(r"(?=<div\b[^>]*class=[\"'][^\"']*job-list-item)", document, flags=re.I)[1:]:
        ad = re.search(r"<a\b[^>]*href=[\"']([^\"']*/ad/[^\"']+)[\"'][^>]*>(.*?)</a>", block, re.I | re.S)
        if not ad:
            continue
        url, body = _clean_url(ad.group(1), base), ad.group(2)
        jobs.append(_new_job("academicpositions", url.rstrip("/").split("/")[-1], _first(r"<h4\b[^>]*>(.*?)</h4>", body), url,
            institutions=[_first(r"<a\b[^>]*href=[\"'][^\"']*/employer/[^\"']+[\"'][^>]*>(.*?)</a>", block)],
            regions=[_first(r"<div\b[^>]*class=[\"'][^\"']*job-locations[^\"']*[\"'][^>]*>(.*?)</div>", block)],
            deadline=_parse_human_date(_first(r"Closing on:\s*([^<]+)", block)),
            created=_parse_human_date(_first(r"Published\s+([^<]+)", block)),
            description=_first(r"<p\b[^>]*class=[\"'][^\"']*text-muted[^\"']*[\"'][^>]*>(.*?)</p>", body)))
    return jobs


def _parse_jobs_ac_uk(document: str, base: str) -> list[dict[str, Any]]:
    jobs = []
    for block in re.split(r"(?=<div\s+id=[\"']doorway-results[\"'])", document, flags=re.I)[1:]:
        tag = re.match(r"<div\b[^>]*>", block, re.I | re.S)
        anchor = re.search(r"<a\b[^>]*href=[\"']([^\"']+/job/[^\"']+)[\"'][^>]*>(.*?)</a>", block, re.I | re.S)
        if not anchor:
            continue
        url = _clean_url(anchor.group(1), base)
        title = _first(r"<h3\b[^>]*>(.*?)</h3>", anchor.group(2)) or html_to_text(anchor.group(2))
        jobs.append(_new_job("jobsacuk", (_attr(tag.group(0), "data-advert-id") if tag else "") or url.rstrip("/").split("/")[-1], title, url,
            institutions=[_first(r"<strong\b[^>]*>(.*?)</strong>", block)],
            regions=[_first(r"(?:Location|Locations):?</[^>]+>\s*(.*?)</(?:li|dd|div)>", block)],
            deadline=_parse_human_date(_first(r">\s*Closes\s*</span>\s*<span\b[^>]*>(.*?)</span>", block), year_if_missing=True),
            created=_parse_human_date(_first(r"<strong\b[^>]*>\s*(?:Placed on|Posted):?\s*</strong>\s*([^<]+)", block))))
    return jobs


def _parse_ajo(document: str, base: str) -> list[dict[str, Any]]:
    jobs = []
    for block in re.split(r"(?=<div\s+class=[\"']clr[\"'])", document, flags=re.I)[1:]:
        heading = re.search(r"<h3\b[^>]*>(.*?)</h3>", block, re.I | re.S)
        names = re.findall(r"<a\b[^>]*>(.*?)</a>", heading.group(1), re.I | re.S) if heading else []
        institution, department = (html_to_text(names[0]) if names else ""), (html_to_text(names[1]) if len(names) > 1 else "")
        for item in re.findall(r"<li\b[^>]*>(.*?)</li>", block, re.I | re.S):
            link = re.search(r"<a\b[^>]*href=[\"']([^\"']*/jobs/(\d+))[\"']", item, re.I)
            if not link:
                continue
            deadline_match = re.search(r"deadline\s+(\d{4}/\d{2}/\d{2})", html_to_text(item), re.I)
            applies = []
            for apply_anchor in re.finditer(r"<a\b[^>]*>(.*?)</a>", item, re.I | re.S):
                if "apply" in html_to_text(apply_anchor.group(1)).casefold():
                    if apply_href := _attr(apply_anchor.group(0), "href"):
                        applies.append(apply_href)
            jobs.append(_new_job("academicjobsonline", link.group(2), _first(r"<span\b[^>]*id=[\"']j\d+[\"'][^>]*>(.*?)</span>", item), _clean_url(link.group(1), base),
                institutions=[institution], description=department,
                deadline=_parse_human_date(deadline_match.group(1)) if deadline_match else None,
                application_urls=[urljoin(base, x) for x in applies]))
    return jobs


def _parse_euraxess(document: str, base: str) -> list[dict[str, Any]]:
    jobs = []
    for block in re.split(r"(?=<div\s+id=[\"']job-teaser-content[\"'])", document, flags=re.I)[1:]:
        link = re.search(r"<a\b[^>]*href=[\"'](/jobs/(\d+))[\"'][^>]*.*?<span>(.*?)</span>.*?</a>", block, re.I | re.S)
        if not link:
            continue
        deadline_match = re.search(r"Application Deadline:.*?<time\b[^>]*datetime=[\"']([^\"']+)", block, re.I | re.S)
        jobs.append(_new_job("euraxess", link.group(2), html_to_text(link.group(3)), urljoin(base, link.group(1)),
            institutions=[_first(r"primary-meta-item[^>]*>\s*<a\b[^>]*>(.*?)</a>", block)],
            regions=[_first(r"Work Locations:.*?ecl-text-standard[^>]*>(.*?)</div>", block)],
            deadline=deadline_match.group(1)[:10] if deadline_match else None,
            created=_parse_human_date(_first(r"Posted on:\s*([^<]+)", block)),
            description=_first(r"ecl-content-block__description[^>]*>(.*?)</div>", block)))
    return jobs


def _parse_cern(document: str, base: str) -> list[dict[str, Any]]:
    jobs = []
    for match in re.finditer(r"<a\b([^>]*)class=[\"'][^\"']*job-offer-teaser[^\"']*[\"']([^>]*)>(.*?)</a>", document, re.I | re.S):
        tag, body = f"<a {match.group(1)} {match.group(2)}>", match.group(3)
        if not (href := _attr(tag, "href")):
            continue
        url, reference = _clean_url(href, base), _first(r"<div\b[^>]*class=[\"'][^\"']*ref[^\"']*[\"'][^>]*>(.*?)</div>", body)
        tags = "; ".join(html_to_text(x) for x in re.findall(r"<li\b[^>]*>(.*?)</li>", body, re.I | re.S))
        jobs.append(_new_job("cern", reference or url.rstrip("/").split("/")[-1], _first(r"<h3\b[^>]*>(.*?)</h3>", body), url,
                             institutions=["CERN"], regions=["Geneva, Switzerland"], description=tags))
    return jobs


def _job_text(job: dict[str, Any]) -> str:
    return " ".join([str(job.get("title", "")), *job.get("institutions", []), str(job.get("description", ""))])


def _fetch_academicjobs(category: str, timeout: float) -> list[dict[str, Any]]:
    jobs = []
    for index in (0, 1):
        document = _request_text(f"https://www.academicjobs.com/sitemap-jobs-{index}.xml", timeout)
        for entry in re.findall(r"<url>(.*?)</url>", document, re.I | re.S):
            url = _first(r"<loc>(.*?)</loc>", entry)
            if not url:
                continue
            title = url.rstrip("/").split("/")[-2].replace("-", " ").title()
            if _relevant(title, category):
                jobs.append(_new_job("academicjobs", url.rstrip("/").split("/")[-1], title, url,
                                     created=_first(r"<lastmod>(.*?)</lastmod>", entry) or None))
    return jobs


def _generic_links(document: str, base: str, source: str, category: str, href_pattern: str) -> list[dict[str, Any]]:
    jobs, seen = [], set()
    for match in re.finditer(r"<a\b[^>]*>(.*?)</a>", document, re.I | re.S):
        href, title = _attr(match.group(0), "href"), html_to_text(match.group(1))
        if not href or not re.search(href_pattern, href, re.I) or not _relevant(title, category):
            continue
        url = _clean_url(href, base)
        if url in seen:
            continue
        seen.add(url)
        jobs.append(_new_job(source, re.sub(r"\W+", "-", url.rstrip("/").split("/")[-1]), title, url))
    return jobs


def _fetch_generic(source: str, url: str, category: str, timeout: float, pattern: str) -> list[dict[str, Any]]:
    document = _request_text(url, timeout)
    if len(document) < 1000 or re.search(r"incapsula|access denied|captcha|just a moment", document, re.I):
        raise RuntimeError("site returned an anti-bot/interstitial page")
    return _generic_links(document, url, source, category, pattern)


def _source_fetchers(category: str, window: str, timeout: float) -> dict[str, Callable[[], list[dict[str, Any]]]]:
    query = quote_plus("particle physics" if category == "hep-ex" else category)
    relevant = lambda jobs: [x for x in jobs if _relevant(_job_text(x), category)]
    return {
        "inspire": lambda: [normalize_job(x) for x in fetch_jobs(category, window, timeout)],
        "physicsworld": lambda: _madgex("physicsworld", "https://www.physicsworldjobs.com/jobs/particle-and-nuclear/", category, timeout, category == "hep-ex"),
        "academicjobs": lambda: _fetch_academicjobs(category, timeout),
        "euraxess": lambda: _relevant_paginated("https://euraxess.ec.europa.eu/jobs/search?f%5B0%5D=job_research_field%3A345", timeout, _parse_euraxess, category),
        "academicpositions": lambda: _relevant_paginated("https://academicpositions.com/jobs/field/physics", timeout, _parse_academic_positions, category),
        "jobsacuk": lambda: _relevant_paginated("https://www.jobs.ac.uk/categories/physics", timeout, _parse_jobs_ac_uk, category),
        "academicjobsonline": lambda: relevant(_parse_ajo(_request_text(f"https://academicjobsonline.org/ajo?action=joblist&args=0-0-0-0--0-40---&id={query}&send=Search", timeout), "https://academicjobsonline.org/ajo")),
        "aps": lambda: _madgex("aps", "https://www.apsphysicsjobs.com/jobs/particle-and-nuclear/", category, timeout, category == "hep-ex"),
        "aas": lambda: _fetch_generic("aas", "https://aas.org/jobregister", category, timeout, r"/jobregister/(?:ad|job)/"),
        "nature": lambda: _madgex("nature", "https://www.nature.com/naturecareers/jobs/physics/", category, timeout),
        "science": lambda: _madgex("science", "https://jobs.sciencecareers.org/jobs/physical-sciences/", category, timeout),
        "findapostdoc": lambda: _fetch_generic("findapostdoc", f"https://www.findapostdoc.com/search/Jobs.aspx?Keywords={query}", category, timeout, r"Job-Details\.aspx|/job/"),
        "higheredjobs": lambda: _fetch_generic("higheredjobs", f"https://www.higheredjobs.com/search/advanced_action.cfm?Keyword={query}", category, timeout, r"job/details\.cfm"),
        "cern": lambda: [x for x in _parse_cern(_request_text("https://careers.cern/jobs/", timeout), "https://careers.cern/jobs/")
                         if _relevant(f"{x['title']} {x['description']}", category)],
    }


def _dedupe_key(job: dict[str, Any]) -> str:
    institution = job.get("institutions", [""])[0] if job.get("institutions") else ""
    value = f"{job.get('title', '')}|{institution}"
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold())


def deduplicate_jobs(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for job in jobs:
        key = _dedupe_key(job) or str(job.get("id"))
        if key not in merged:
            merged[key] = job
            continue
        target = merged[key]
        for field in ("institutions", "ranks", "regions", "experiments", "sources", "source_urls", "application_urls"):
            target[field] = list(dict.fromkeys([*target.get(field, []), *job.get(field, [])]))
        if len(job.get("description", "")) > len(target.get("description", "")):
            target["description"] = job["description"]
        target["deadline"] = target.get("deadline") or job.get("deadline")
        target["created"] = target.get("created") or job.get("created")
    return list(merged.values())


def deadline_key(job: dict[str, Any]) -> tuple[date, str]:
    return _parse_deadline(job.get("deadline")) or date.max, str(job.get("title", "")).casefold()


def filter_jobs(jobs: Iterable[dict[str, Any]], window: str, ranks: list[str]) -> list[dict[str, Any]]:
    cutoff = subtract_months(date.today(), int(window[:-1])) if window != "current" else None
    requested, selected = {x.upper() for x in ranks}, []
    for job in jobs:
        deadline = _parse_deadline(job.get("deadline"))
        current = job.get("status") == "open" and (deadline is None or deadline >= date.today())
        if window == "current" and not current:
            continue
        if cutoff and not current and (deadline is None or deadline < cutoff):
            continue
        if requested and not requested.intersection(job.get("ranks", [])):
            continue
        selected.append(job)
    return sorted(selected, key=deadline_key)


def group_jobs(jobs: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in jobs:
        for experiment in job.get("experiments") or [UNTAGGED]:
            grouped[experiment].append(job)
    for values in grouped.values():
        values.sort(key=deadline_key)
    return dict(grouped)


def _ordered_groups(groups: dict[str, list[dict[str, Any]]]) -> list[tuple[str, list[dict[str, Any]]]]:
    return sorted(groups.items(), key=lambda x: (x[0] == UNTAGGED, -len(x[1]), x[0].casefold()))


def render_text(jobs: list[dict[str, Any]], category: str, details: bool, width: int,
                source_counts: dict[str, int] | None = None, warnings: list[str] | None = None) -> str:
    lines = [f"Current {category} advertisements: {len(jobs)}"]
    if source_counts is not None:
        lines.append("Sources: " + ", ".join(f"{SOURCE_LABELS[x]} {n}" for x, n in source_counts.items()))
    if warnings:
        lines.append(f"Source warnings: {len(warnings)} (also written to stderr)")
    lines.extend(("", "ADVERTISEMENTS BY EXPERIMENT"))
    for experiment, advertisements in _ordered_groups(group_jobs(jobs)):
        lines.extend(("", f"== {experiment} ({len(advertisements)}) =="))
        for index, job in enumerate(advertisements, 1):
            facts = [" / ".join(x.title() for x in job["ranks"])] if job["ranks"] else []
            if job["regions"]:
                facts.append(", ".join(job["regions"]))
            facts.append(f"deadline {job['deadline'] or 'not specified'}")
            lines.extend((f"\n{index}. {job['title']} — {', '.join(job['institutions']) or 'Institution not specified'}",
                          f"   {' | '.join(facts)}", f"   Source:  {', '.join(job['sources'])}"))
            lines.extend(f"   Listing: {url}" for url in job["source_urls"])
            lines.extend(f"   Apply:   {url}" for url in job["application_urls"])
            if details and job["description"]:
                lines.extend(("   Description:", textwrap.fill(job["description"].replace("\n", " "), width=width,
                              initial_indent="   ", subsequent_indent="   ")))
    return "\n".join(lines).rstrip() + "\n"


def render_json(jobs: list[dict[str, Any]], category: str, window: str, ranks: list[str],
                source_counts: dict[str, int] | None = None, warnings: list[str] | None = None) -> str:
    return json.dumps({"fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "category": category, "window": window, "ranks": ranks,
        "source_counts_before_deduplication": source_counts or {}, "warnings": warnings or [],
        "unique_advertisement_count": len(jobs),
        "groups": [{"experiment": name, "count": len(ads), "advertisements": ads}
                   for name, ads in _ordered_groups(group_jobs(jobs))]}, indent=2, ensure_ascii=False) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect physics jobs from INSPIRE and academic job boards.")
    parser.add_argument("--category", default="hep-ex", help="physics/arXiv relevance category (default: hep-ex)")
    parser.add_argument("--source", nargs="+", choices=ALL_SOURCES, default=list(ALL_SOURCES), help="sources (default: all)")
    parser.add_argument("--list-sources", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="maximum unique results; 0 means all")
    parser.add_argument("--window", choices=("current", "3m", "6m"), default="current")
    parser.add_argument("--rank", nargs="+", choices=("postdoc", "senior"), metavar="{postdoc,senior}")
    parser.add_argument("--details", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--width", type=int, default=100)
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
    if args.list_sources:
        for key, label in SOURCE_LABELS.items():
            print(f"{key:22} {label}")
        return 0
    counts, warnings, collected = {}, [], []
    fetchers = _source_fetchers(args.category, args.window, args.timeout)
    for source in dict.fromkeys(args.source):
        try:
            source_jobs = fetchers[source]()
            counts[source] = len(source_jobs)
            collected.extend(source_jobs)
        except PartialSourceError as exc:
            counts[source] = len(exc.jobs)
            collected.extend(exc.jobs)
            warning = f"{SOURCE_LABELS[source]}: partial results retained; {exc}"
            warnings.append(warning)
            print(f"warning: {warning}", file=sys.stderr)
        except (RuntimeError, OSError) as exc:
            counts[source] = 0
            warning = f"{SOURCE_LABELS[source]}: {exc}"
            warnings.append(warning)
            print(f"warning: {warning}", file=sys.stderr)
    jobs = filter_jobs(deduplicate_jobs(collected), args.window, args.rank or [])
    if args.limit:
        jobs = jobs[:args.limit]
    output = (render_json(jobs, args.category, args.window, args.rank or [], counts, warnings) if args.json else
              render_text(jobs, args.category, args.details, args.width, counts, warnings))
    try:
        args.output.write_text(output, encoding="utf-8") if args.output else sys.stdout.write(output)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0 if collected else 1


if __name__ == "__main__":
    raise SystemExit(main())
