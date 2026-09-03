import json
import unittest
from datetime import date, timedelta
from unittest.mock import patch

import inspire_jobs_by_experiment as jobs


class CollectorTests(unittest.TestCase):
    def test_html_to_text(self):
        self.assertEqual(jobs.html_to_text("<p>Hello &amp; <b>world</b></p>"), "Hello & world")

    def test_relevance_uses_word_boundaries(self):
        self.assertFalse(jobs._relevant("Institute of Geodesy", "hep-ex"))
        self.assertTrue(jobs._relevant("Researcher for the DESY ATLAS group", "hep-ex"))

    def test_madgex_parser(self):
        page = '''<li class="lister__item cf" id="item-42">
          <h3 class="lister__header"><a href="/job/42/test/"><span>Particle Physics Postdoc</span></a></h3>
          <li class="lister__meta-item--recruiter">Example Lab</li>
          <li class="lister__meta-item--location">Geneva</li>
          <p class="lister__description">Detector research</p></li>'''
        parsed = jobs._parse_madgex(page, "https://example.test/jobs/", "physicsworld")
        self.assertEqual(parsed[0]["title"], "Particle Physics Postdoc")
        self.assertEqual(parsed[0]["institutions"], ["Example Lab"])
        self.assertEqual(parsed[0]["ranks"], ["POSTDOC"])

    def test_ajo_parser(self):
        page = '''<div class="clr"><h3 class="x1"><a>University</a>, <a>Particle Physics</a></h3><ol>
          <li>[<a href="/ajo/jobs/123">PD</a>] <span id="j123">Postdoctoral Researcher</span>
          (deadline 2099/12/31 11:59PM) <a href="/ajo/jobs/123/apply">Apply</a></li></ol></div>'''
        parsed = jobs._parse_ajo(page, "https://academicjobsonline.org/ajo")
        self.assertEqual(parsed[0]["deadline"], "2099-12-31")
        self.assertIn("https://academicjobsonline.org/ajo/jobs/123/apply", parsed[0]["application_urls"])

    def test_deduplication_merges_sources(self):
        one = jobs._new_job("physicsworld", "1", "Detector Scientist", "https://one", institutions=["Lab"])
        two = jobs._new_job("aps", "2", "Detector Scientist", "https://two", institutions=["Lab"])
        merged = jobs.deduplicate_jobs([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sources"], ["Physics World Jobs", "APS Physics Jobs"])

    def test_current_filter_removes_expired(self):
        expired = jobs._new_job("cern", "x", "Physicist", "https://x",
                                deadline=(date.today() - timedelta(days=1)).isoformat())
        self.assertEqual(jobs.filter_jobs([expired], "current", []), [])

    def test_json_has_source_metadata(self):
        output = json.loads(jobs.render_json([], "hep-ex", "current", [], {"inspire": 0}, ["warning"]))
        self.assertEqual(output["source_counts_before_deduplication"], {"inspire": 0})
        self.assertEqual(output["warnings"], ["warning"])

    def test_all_requested_sources_are_registered(self):
        self.assertEqual(len(jobs.ALL_SOURCES), 14)

    def test_later_page_failure_retains_partial_results(self):
        page = '<a rel="next" href="/page-2">next</a>'
        with patch.object(jobs, "_request_text", side_effect=[page, RuntimeError("rate limited")]):
            with self.assertRaises(jobs.PartialSourceError) as raised:
                jobs._fetch_paginated("https://example.test/page-1", 1, lambda document, url: [{"id": "one"}])
        self.assertEqual(raised.exception.jobs, [{"id": "one"}])


if __name__ == "__main__":
    unittest.main()
