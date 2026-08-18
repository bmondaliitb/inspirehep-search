# INSPIRE HEP jobs by experiment

`inspire_jobs_by_experiment.py` fetches active job advertisements from the
[INSPIRE Jobs API](https://inspirehep.net/api/jobs), selects `hep-ex` jobs, and
groups the advertisements by their explicitly tagged experiments. Jobs tagged
with several experiments are shown in every applicable group; relevant jobs
without an experiment tag are kept under **Unspecified experiment**.

No API key or third-party Python package is required. Python 3.9 or newer is
recommended.

```bash
# Show every active hep-ex advertisement
python3 inspire_jobs_by_experiment.py

# Include each advertisement's full description
python3 inspire_jobs_by_experiment.py --details

# Save structured data (descriptions are always included in JSON)
python3 inspire_jobs_by_experiment.py --json --output hep-ex-jobs.json

# Quick preview of the first 10 advertisements
python3 inspire_jobs_by_experiment.py --limit 10

# Current openings plus advertisements with deadlines in the last 3 months
python3 inspire_jobs_by_experiment.py --window 3m

# Current openings plus the last 6 months, limited to postdocs and senior jobs
python3 inspire_jobs_by_experiment.py --window 6m --rank postdoc senior
```

The default terminal output includes the title, institution, rank, region,
deadline, INSPIRE record, and external application link. Run
`python3 inspire_jobs_by_experiment.py --help` for all options.
