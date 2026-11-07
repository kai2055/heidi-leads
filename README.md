# heidi-leads

Lead pipeline that finds property management companies (Hausverwaltungen) in Berlin and scores them as potential customers for a digital metering service provider.

Built for a case study. Work in progress.

## Pipeline

| Step | What it does | Status |
|---|---|---|
| 1. Collect | Find companies on Google Maps across a grid covering Berlin | done (`collect_places.py`) |
| 2a. Filter | Label each place: likely property manager, unclear, competitor, or excluded (with reason) | done (`filter_candidates.py`) |
| 2b. Details | Add website and phone for likely and unclear candidates | planned |
| 3. Crawl | Save each company's home, Impressum, services, careers and about pages | planned |
| 4. Extract | An LLM fills the KPI columns, each answer with a quote and source link | planned |
| 5. Check | Reject any answer whose quote is not on the source page | planned |
| 6. Score | Rule-based Fit and Ease scores, then an A/B/C grade with a reason | planned |
| 7. Validate | Hand-check a random sample and score known customers | planned |

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
python collect_places.py                  # full run
```

## Tests

```bash
pytest -q
```

The tests use a fake Google API and cost nothing. They cover the grid, tile splitting, Berlin filtering, duplicate handling, the hard call limit, error handling and raw logging.

## Data policy

The API key (`.env`) and all collected data (`data/`) are excluded from the repository by `.gitignore`. Only code and documentation are public.