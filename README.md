# Physics jobs by experiment

`inspire_jobs_by_experiment.py` collects current physics vacancies from INSPIRE
and major academic job boards, filters them for the requested physics category,
merges duplicate advertisements, and groups jobs by explicit INSPIRE experiment
tags. Jobs without an experiment tag remain under **Unspecified experiment**.

The collector has no third-party dependencies. A source that is temporarily
inaccessible produces a warning while all other sources continue normally.

## Sources

- [INSPIRE](https://inspirehep.net/)
- [Physics World Jobs](https://www.physicsworldjobs.com/)
- [AcademicJobs.com](https://www.academicjobs.com/)
- [EURAXESS](https://euraxess.ec.europa.eu/jobs)
- [Academic Positions](https://academicpositions.com/jobs/field/physics)
- [jobs.ac.uk](https://www.jobs.ac.uk/categories/physics)
- [AcademicJobsOnline](https://academicjobsonline.org/ajo)
- [APS Careers](https://www.aps.org/careers) via APS Physics Jobs
- [AAS Job Register](https://aas.org/jobregister)
- [Nature Careers](https://www.nature.com/careers)
- [Science Careers](https://www.science.org/careers)
- [FindAPostDoc](https://www.findapostdoc.com/)
- [HigherEdJobs](https://www.higheredjobs.com/)
- [CERN Careers](https://careers.cern/jobs/)

Use `--list-sources` for the corresponding CLI keys.

```bash
# All sources; current experimental/high-energy physics jobs
python3 inspire_jobs_by_experiment.py

# Selected sources only
python3 inspire_jobs_by_experiment.py --source inspire physicsworld academicjobs

# Descriptions or structured JSON
python3 inspire_jobs_by_experiment.py --details
python3 inspire_jobs_by_experiment.py --json --output hep-ex-jobs.json

# Current openings plus recently closed ads; optional rank filter
python3 inspire_jobs_by_experiment.py --window 3m --rank postdoc senior

# Another relevance profile
python3 inspire_jobs_by_experiment.py --category astro-ph --source inspire aas nature
```

The default `hep-ex` profile recognizes particle/high-energy and experimental
physics, accelerators, detectors, major laboratories, and experiments. The
Physics World and APS particle/nuclear categories are accepted as already
focused; broad physics boards are filtered using listing metadata.

Some boards deploy network-dependent anti-automation pages. The collector does
not bypass them: it records the source warning in stderr and JSON and retains
the results from every other source.
