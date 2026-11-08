# heidi-leads

Lead pipeline that finds property management companies (Hausverwaltungen) in Berlin and scores them as potential customers for a digital metering service provider.

Built for a case study. Work in progress.

## Pipeline

| Step | What it does | Status |
|---|---|---|
| 1. Collect | Find companies on Google Maps across a grid covering Berlin | done (`collect_places.py`) |
| 2a. Filter | Label each place: likely property manager, unclear, competitor, or excluded (with reason) | done (`filter_candidates.py`) |
| 2b. Details | Add website and phone: all likely candidates plus a random sample of unclear ones, within the free allowance | done (`fetch_details.py`) |
| 3. Crawl | Save each company's home, Impressum, services, about and careers pages as clean text | done (`crawl_websites.py`) |
| 4. Extract | An LLM (via OpenRouter) fills the KPI facts, each answer with a quote and source link | done (`extract_kpis.py`) |
| 5. Check | Reject any answer whose quote is not on the source website | done (inside `extract_kpis.py`) |
| 6. Score | Rule-based Fit and Ease scores, then an A/B/C/U grade with every point explained | done (`score_leads.py`) |
| 7. Validate | Golden set for the extraction, random hand check of final grades | done (`compare_models.py`, `validate_sample.py`) |

## Step 1: collect

Google Places Text Search returns at most 60 results per search, so one search for all of Berlin would miss most companies. The script therefore:

- searches 4 terms: Hausverwaltung, Immobilienverwaltung, WEG-Verwaltung, Mietverwaltung
- starts with a 4 x 4 grid over Berlin (tiles of roughly 9 x 11 km)
- splits any tile that hits the 60-result cap into 4 smaller tiles (a quadtree), up to 3 times (smallest tile roughly 1 x 1.4 km)
- reports `tiles_still_capped` in the log; 0 means no area was too crowded to cover

The 4 x 4 start is a starting choice, not a tuned value. The split rule corrects it where companies are dense.

A full tile with no Berlin company in it (for example around Potsdam) is not split further, so calls are not spent outside Berlin. Trade-off: a tile on the city border whose first 60 results are all outside Berlin is not explored deeper.

## Step 2a: filter

Google matches search terms loosely. In the first test run, only about 40% of Berlin results were clearly property managers; the rest were estate agents, builders, a plumber, a lawyer, a care home and one metering company. `filter_candidates.py` labels every row and never deletes anything:

| Label | Meaning |
|---|---|
| `likely_pm` | Name (or company name in the address) says property management |
| `unclear` | Could be a property manager, estate agent or housing company; the website check decides |
| `competitor` | Metering service provider (ista, Techem, KALO, ...): not a lead |
| `excluded` | Outside Berlin, permanently closed, a building listing, or a wrong trade |

Each row also gets `relevance_reason` and a `shared_address` flag (several listings at one address, e.g. an estate agent and a property manager run from the same office).

```bash
python filter_candidates.py
```

## Step 2b: details (website and phone)

Place Details with website and phone is billed at the Enterprise tier, with 1,000 free calls per month. To stay inside that:

- every `likely_pm` candidate is looked up
- the rest of the 1,000 is a random sample of `unclear` candidates (fixed seed 42, so it is repeatable)
- the sample shows what share of the unclear group are real property managers, so the rest can be estimated and finished later if the cost is justified

Safety: a hard stop per run (`--max-calls`), and resume: places already in `data/details.csv` are never fetched again.

```bash
python fetch_details.py --max-calls 5   # small test run
python fetch_details.py                 # full run (continues where the test stopped)
```

Output: `data/enriched_candidates.csv` (every candidate, with `selection_group`, website, phone and Google rating).

## Step 3: crawl websites

For each selected company with a website, `crawl_websites.py` saves up to 5 pages as clean text: the homepage plus the Impressum, services, about/team and careers pages, found by words in their links (German and English).

- Respects `robots.txt`; an honest user agent links to this repository.
- At most 1 request per second per website; 8 websites in parallel.
- Companies sharing one website are crawled once.
- "Websites" that are really Facebook, ImmoScout or similar profile pages are skipped.
- TLS certificates are always verified; broken ones are recorded as `ssl_error`.
- Sites with almost no text (usually JavaScript-only) are marked `low_text`.
- Resume: websites already in `data/crawl_status.csv` are skipped.

```bash
python crawl_websites.py --limit 10   # small test run
python crawl_websites.py              # all websites
```

Output: `data/pages.jsonl` (one line per page: text, URL, page type, e-mail addresses shown) and `data/crawl_status.csv` (one row per website).

## Steps 4 and 5: AI extraction with quote check

`extract_kpis.py` sends each website's saved pages to an LLM through OpenRouter and asks 15 fixed questions (property manager or not, WEG / rental / SEV services, units managed, tenant portal, online repair form, software, metering provider, hiring, growth, association membership, managing directors, legal form).

- Every answer must include a short quote copied from the page and the page URL, or be "unknown".
- Python then checks every quote against the saved page text. If the quote is not on the website, or a unit count is not in its quote, the answer is reset to "unknown" and listed in `failed_checks`.
- Website text is passed as user data, and the system prompt says never to follow instructions inside it.
- Strict JSON schema output, with a fallback to plain JSON mode for models that do not support schemas.
- Hard stop on cost (`--max-cost`, USD), resume per model, 4 requests in parallel.

Model choice is tested, not assumed: the same fixed random sample of websites (`--sample`, seed 7) is run on each candidate model, and the answers are compared and hand-checked before the full run.

```bash
python extract_kpis.py --model google/gemini-2.5-flash-lite --sample 20
python extract_kpis.py --model <second model id> --sample 20
python extract_kpis.py --model <chosen model id>        # all websites
```

Output per model: `data/kpis_<model>.csv`, `data/extractions_<model>.jsonl` (raw answers, checks, tokens, cost) and `data/extract_log_<model>.json`.

### Cost safety

- Requests only Pro-tier fields (no website, phone or rating), which keeps calls on the cheaper SKU with a larger free monthly allowance.
- Hard stop: the script never makes more than `--max-calls` requests (default 1000).
- The Google Cloud project also has daily quotas and a budget alert.

### Outputs (in `data/`, not committed)

- `raw_places.csv`: one row per unique place, with the search terms that found it
- `collect_log.json`: calls used, tiles searched and split, counts
- `raw/search_responses_<run_id>.jsonl`: every request and raw response, so each row can be traced back to its source

## Setup

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env              # then paste your Google Places API key into .env
```

## Run

```bash
python collect_places.py --max-calls 20   # small test run
python collect_places.py --max-depth 4    # full run, one extra split level for crowded areas
```

## Tests

```bash
pytest -q
```

The tests use a fake Google API and cost nothing. They cover the grid, tile splitting, Berlin filtering, duplicate handling, the hard call limit, error handling and raw logging.

## Data policy

The API key (`.env`) and all collected data (`data/`) are excluded from the repository by `.gitignore`. Only code and documentation are public.

## Step 6: scoring

`score_leads.py` gives every company a status (lead, competitor, excluded, with the reason), a lead type
(property manager, housing company, unverified) and, for leads with a readable website, a score out of 100:

| Part | KPI | Points |
|---|---|---|
| Fit (65) | Size: units stated (<100: 5, 100-499: 12, 500-2,999: 20, 3,000+: 25); otherwise 30+ Google reviews = 10 ("probably large") | 25 |
| | Residential focus: WEG / rental / SEV confirmed or housing company 25, name only 12, commercial only 0 | 25 |
| | Digital: tenant portal 4, online repair form 3, software named 3 | 10 |
| | Metering hook: names its current metering provider | 5 |
| Ease (35) | No owners' vote needed (rental management or owns the flats) | 10 |
| | Owner-run (e.K., GbR, Einzelunternehmen, Inhaber) | 10 |
| | Hiring (weak signal) | 5 |
| | Association: VDIV (Heidi partner) 10, other 5 | 10 |

Grades: A >= 60, B 35-59, C < 35, U = unverified (no readable website; not graded, sorted by Google reviews).
Every row has a `score_reason` column listing each point and why.

Design decisions backed by tests on the data:
- Google reviews as size proxy: Spearman 0.54 (n = 103), but they only separate very large firms
  (median 56 reviews at 3,000+ units vs 9-12 below), so reviews give at most 10 points (`analyze_size_proxy.py`).
- Version 2 of the weights after hand validation: size no longer penalises very large firms (slowness is
  already in Ease), and hiring was halved after 2 of 10 hand-checked A-leads showed no visible open jobs.

## Step 7: validation

- Extraction: model and prompt choice scored against a hand-checked golden set (22 hard cases).
- Final grades: 30 random rows (10 A, 10 C, 10 excluded by the AI), checked by hand on the live websites
  (`validate_sample.py`).

## Step 8: final workbook

`build_sheet.py` writes `data/heidi_leads_berlin.xlsx` with five tabs: Leads (graded A/B/C with contact,
KPIs and the reason for every point), Unverified, Excluded (competitors first, each with a reason),
KPI definitions, and Method & numbers (funnel, validation and costs from the run logs).

Contacts are only published business data (Impressum and website), each with its source link. E-mail
addresses are never guessed. LinkedIn profiles are checked by hand from a prepared search link;
LinkedIn itself is not scraped.

```bash
python compare_models.py --eval google/gemini-3.1-flash-lite   # saves golden_eval.json for the method tab
python score_leads.py google/gemini-3.1-flash-lite
python build_sheet.py
```