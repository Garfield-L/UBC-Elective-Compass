# UBC Elective Compass

UBC Elective Compass is a local Version 1 course-discovery application for UBC
Vancouver undergraduate students. It combines a bounded 60-subject catalog,
historical course averages where available, subject-based interest tags, a
FastAPI search API, and a framework-free browser interface.

## Catalog extraction history

The data foundation begins with a small Python component that downloads one
UBC Vancouver Academic Calendar **subject page** and converts its undergraduate
course descriptions into UTF-8 JSON. It does not provide registration,
prerequisite checking, live availability, or degree advising.

## Source and responsible use

The source is the official [UBC Vancouver Academic Calendar — Courses by
Subject](https://vancouver.calendar.ubc.ca/course-descriptions/courses-subject).
The current command defaults to **ADHE_V (Adult and Higher Education)** only.
It uses an identifying user agent, a 20-second request timeout, and includes a
one-second polite-delay helper for future multi-page work. It does not retry,
bypass access controls, or crawl the complete catalog.

## Output schema

Each undergraduate course record has this shape:

```json
{
  "course_code": "ADHE 327",
  "subject": "ADHE",
  "course_number": 327,
  "title": "Teaching Adults",
  "description": "Planning, conducting and evaluating instruction for adults.",
  "credits": 3,
  "credits_raw": null,
  "level": 300,
  "faculty_school": "Faculty of Education",
  "source_url": "https://vancouver.calendar.ubc.ca/course-descriptions/subject/adhev"
}
```

`credits` is `null` when the Calendar uses an unusual (for example,
variable-credit) format. In that case, `credits_raw` preserves the exact
Calendar value (for example, `"3-6"`) and the script prints a warning instead
of inventing a number. Fixed decimal credits such as `1.5` remain numeric.
Schedule notation such as `[3-0-0]` is removed from descriptions.
Courses outside 100–499 are reported and excluded from the undergraduate output.
`faculty_school` is `null` unless the subject-page heading states it clearly.

## Setup and run

Python 3.10+ is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python src/scrape_courses.py
```

The default output is `data/adhe_courses.json`. To select one other subject
explicitly (still only one page), provide its exact official Calendar URL:

```bash
python src/scrape_courses.py \
  --subject-url https://vancouver.calendar.ubc.ca/course-descriptions/subject/cpscv \
  --output data/cpsc_courses.json
```

To run the offline parser tests:

```bash
python -m unittest discover -s tests -v
```

## Fixed 20-subject validation run

For a controlled validation sample (not a full crawl), run:

```bash
python src/validate_subjects.py
```

The runner fetches the official master subject page once, resolves a fixed list
of 20 diverse subject codes from that page, and visits only those subject pages
sequentially with the polite delay between requests. It writes:

* `data/step6_combined_courses.json` — combined undergraduate records for the sample
* `data/step6_validation_report.json` — per-subject metrics, malformed-heading
  examples, and automated invariant results

It deliberately does not discover and crawl the rest of the catalog.

## Version 1 catalog build (60 subjects)

The bounded Version 1 coverage is defined in
`config/subjects_v1.txt`. It contains exactly 60 official Calendar subject
codes and is intentionally not the full UBC catalog.

```bash
python src/build_v1_catalog.py
```

The build resolves each configured code from the master subject page, fetches
only those 60 pages sequentially, and writes:

* `data/ubc_courses_v1.json` — the combined undergraduate course catalog
* `data/ubc_courses_v1_report.json` — validation results, counts by subject,
  level, and faculty/school

## Design notes and limitations

`src/scrape_courses.py` contains separate functions to find subject links from
the master page, parse a supplied subject page, validate records, and write JSON.
This makes the next approved stage (a polite multi-subject runner) possible
without mixing it into the present command.

The full UBC catalog has **not** been scraped. The parser assumes the Calendar's
current course teaser structure (`article.node--type-course` with an `h3` and
paragraph). It warns and skips malformed headings or duplicate course codes so
one bad entry does not stop a run. If UBC materially changes its HTML, the
selector/parser may need an update.

## Step 8A grade-data investigation

This bounded experiment does not enrich the catalog. It asks the public
[UBCGrades v3 API](https://ubcgrades.com/api-reference/v3) for six specified
courses in `2025W`, then performs only a small fallback search among those same
courses if one has no usable 2025W aggregate. It writes a diagnostic report:

```bash
python src/grade_validation.py
```

The report is `data/step8a_grade_investigation.json`. A selected value must be
an API-provided `OVERALL` row with a numeric `average` and positive `reported`
count for the exact three-digit course. A Reported-weighted section average is
included only as a diagnostic: overlapping lecture/lab section populations can
otherwise double-count students. Four-character API identifiers such as `230A`
remain distinct from `230`. The experiment also reads one CPSC course-label
response to record real detail modifiers without fetching grades for the
subject's full course list.

## Step 8B controlled grade-enrichment sample

Step 8B selects exactly five courses from each of 20 approved Version 1
subjects, without changing the base catalog. The deterministic sampler first
selects a median course from each available 100/200/300/400 level, then fills
remaining slots with the median of the remaining ordered courses.

```bash
python src/enrich_grade_sample.py
```

It writes these separate, ignored data artifacts:

* `data/step8b_sample_courses.json` — the exact 100 selected catalog records
* `data/step8b_grade_enriched_sample.json` — the 100 records with grade fields
* `data/step8b_grade_report.json` — coverage, age, batching, and validation results

The runner dynamically retrieves sessions, processes them newest-first, and
uses the subject/session batch endpoint once at most for each needed pair. A
selected grade requires an exact empty-detail base course with one usable API
`OVERALL` row; detail variants are recorded but never merged into the base
course.

## Full Version 1 grade enrichment (local execution)

The full runner is prepared but is not run automatically. From the repository
root, run this command when you are ready to perform the complete enrichment:

```bash
python3 src/enrich_full_catalog.py
```

It leaves `data/ubc_courses_v1.json` unchanged and writes only after all final
invariants pass:

* `data/ubc_courses_v1_with_grades.json` — all original catalog records plus grade fields
* `data/full_grade_enrichment_report.json` — coverage, session, subject, retry, error, and validation counts
* `data/full_grade_enrichment_checkpoint.json` — atomic, per-subject progress for resuming an interrupted run

The checkpoint is saved after every completed subject. Rerun the same command
to resume without repeating completed subjects. If the catalog or available
API-session list has changed, the runner stops safely and asks you to restart.
To deliberately discard the checkpoint's progress and begin a fresh run, use:

```bash
python3 src/enrich_full_catalog.py --restart
```

The runner makes sequential subject/session batch requests and reuses each
response for every course in that subject. It retries transient failures twice
by default, but treats a subject/session 404 as normal absence of data and does
not retry it. A remaining network failure is recorded as `fetch_error`, never
as `no_grade_history`; that subject is deliberately left out of the completed
checkpoint set, so a normal rerun retries only it. Successful completion means
the command exits with code zero, `validation_errors` in the report is an empty
list, and `fetch_error` is zero.

## Version 1 interest tagging

Version 1 tags courses only through the fixed subject mapping in
`config/subject_interest_map.json`. It never reads titles or descriptions for
classification. Each course receives exactly one future-compatible list value,
such as `"interest_tags": ["Technology & Computing"]`.

```bash
python3 src/tag_interest_categories.py
```

The source `data/ubc_courses_v1_with_grades.json` remains unchanged. A
successful local run writes `data/ubc_courses_v1_final.json` and
`data/interest_tagging_report.json`. The report includes mapping validation,
category counts, and the few subject-level decisions that merit review.

## Phase 2 course filtering

`src/filter_courses.py` is a pure-Python, reusable filtering module for the
final Phase 1 dataset. It does not modify the source JSON. Its
`filter_courses(...)` function supports exact approved interest categories
(OR matching), 300/400-level-only filtering, and a high historical-average
filter of 80.0% or greater. Returned copies include only temporary
`matched_interests` and `matched_interest_count` fields.

```bash
python3 src/filter_courses.py
```

This validates the nine required real-data searches and writes the ignored
`data/phase2_filter_validation_report.json`. All results use deterministic
sorting: interest match count first when interests are selected, then a grade
average when the high-average filter is enabled, and finally course code.

## Current application

UBC Elective Compass is a local, framework-free course-discovery application
for a 5,722-course UBC Vancouver undergraduate dataset. It covers all 264
current Calendar subject pages discovered by the pipeline, with 186 subjects
contributing at least one 100--499-level course. Its FastAPI backend loads
`data/ubc_courses_full_final.json` once at startup; the browser never uses a
mock course list or a database.

The current application includes:

* backend-ranked search by course code or subject, including partial entries
  and the common aliases `CS`, `COMPUTER SCIENCE`, `COMP SCI`, and `STATS`;
* 18 API-supplied interest categories, with OR matching for multiple selected
  interests;
* 300/400-level and historical-average (80.0%+) filters;
* globally applied course-code or highest-average sorting, followed by
  20-course Load More pagination;
* browser-local Saved Courses; and
* a four-step onboarding guide that Help can reopen.

### Run locally

Install dependencies once, then run the backend and frontend in separate
terminals:

```bash
# From the project root
python3 -m pip install -r requirements.txt
python3 -m uvicorn src.api:app --reload

# From the frontend directory
cd frontend
python3 -m http.server 5500
```

Open <http://127.0.0.1:5500>. If that port is busy, run the frontend on port
5501 instead; the local API CORS configuration supports both ports. The API is
available at <http://127.0.0.1:8000>, with interactive documentation at
<http://127.0.0.1:8000/docs>.

### API summary

* `GET /health` returns the startup-loaded course count.
* `GET /interests` returns the exact 18 approved interest categories.
* `POST /courses/search` accepts `query`, `interests`, `higher_level`,
  `high_gpa`, `sort_by` (`course_code` or `highest_average`), `limit` (1–200),
  and `offset`. Filtering and sorting happen before pagination.
* `GET /stats/interactions` reads the optional global interaction total, while
  `POST /stats/interactions` accepts only `visit`, `search`, or `save` and
  atomically records one interaction.

For example:

```bash
curl -X POST http://127.0.0.1:8000/courses/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"CPSC 320","interests":["Technology & Computing"],"high_gpa":true,"sort_by":"highest_average"}'
```

### Browser-local state

Saved Courses uses the versioned `ubcElectiveCompassSavedCoursesV2`
`localStorage` key and stores real course records only in the current browser.
The onboarding guide separately records whether it has been dismissed. Neither
feature writes to the backend or shares data between browsers.

### Interaction counter configuration

Persistent global interaction counting requires the server-side Render
environment variables `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN`.
The Upstash counter is seeded to 100 only when its key is absent and records
only visits, explicit non-empty searches, and new saves. The browser never
receives the token. Without those variables or during a datastore outage, the
counter displays `—`; course discovery remains available.

### Current limitations

* The catalog covers courses numbered 100--499 from all current UBC Vancouver
  Calendar subject pages discovered by this pipeline. It intentionally excludes
  below-100 and 500+ courses and does not claim to include every course offered
  by UBC in every session.
* Interest tags are subject-based, not course-content classifications.
* Historical averages may come from different available sessions and are not
  current enrollment information.
* This is not official UBC advising or degree-planning guidance.
* It does not check prerequisites, current seat availability, online/in-person
  delivery, or section times.
* Saved Courses are browser-local and are not user accounts or cloud storage.

## Phase 7A deployment preparation

This repository is prepared for manual deployment, but does not deploy itself.

### Render backend

Create a Python 3 web service from the repository root with these settings:

* Build command: `pip install -r requirements.txt`
* Start command: `uvicorn src.api:app --host 0.0.0.0 --port $PORT`
* Environment variable: set `ALLOWED_ORIGINS` to the exact Vercel site origin
  once it exists. Multiple explicit origins can be comma-separated. Do not use
  `*`.

The `.python-version` file requests Python 3.14.7. The startup-loaded dataset
uses a `pathlib` path relative to the repository, so
`data/ubc_courses_full_final.json` is available when the repository is deployed.

### Vercel static frontend

Deploy the `frontend/` directory as the Vercel Root Directory. It is a plain
static site: no framework preset, build command, or output-directory setting is
needed. `index.html` references `styles.css` and `app.js` with relative paths.

Before deploying the frontend, replace the one `API_BASE_URL` value near the
top of `frontend/app.js` with the HTTPS URL of the deployed Render backend, then
redeploy the static site. Saved Courses and onboarding remain browser-local
through `localStorage`.

### Dataset and Git

The full production dataset is explicitly allowed by `.gitignore`:
`data/ubc_courses_full_final.json`. The historical V1 dataset remains tracked;
candidate, checkpoint, and report JSON artifacts remain ignored.
