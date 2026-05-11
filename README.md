# AI-Powered Claim Denial Analysis — RCM

A Python pipeline for analyzing denied healthcare claims using joined EDI 835/837 data. Takes a set of claims, figures out why each one was denied, estimates whether it's worth appealing, finds similar historical paid claims for context, and groups denials into actionable batches.

No paid APIs required to run — works completely offline against the synthetic dataset.

---

## Setup

```bash
pip install -r requirements.txt   # if you're using the LLM path
python generate_synthetic_data.py  # creates data/synthetic_claims.json
python main.py
```

Results land in `outputs/`:
- `claim_analyses.json` — per-claim verdict, evidence, recommended actions
- `denial_clusters.json` — denials grouped by payer/code/procedure, sorted by recoverable dollars
- `evaluation.json` — accuracy against expected labels in the synthetic dataset

To analyze just one claim:
```bash
python main.py --claim-id CLM-2026-00391
```

---

## How it works

The core idea is straightforward: for each denied claim, join the 835 (what the payer rejected) with the 837 (what we originally billed), then run it through denial-type-specific logic to decide if the denial holds up.

**Step 1 — Field normalization.** Claims can come in nested format (`edi_835` / `edi_837` blocks) or flat legacy format. A small set of helpers reads from whichever structure is present.

**Step 2 — Historical matching.** Before analyzing a claim, look at similar historical claims (same payer, same procedure, similar diagnoses, same provider). If the payer paid similar claims before, that weakens a firm denial verdict and strengthens the case for an appeal.

**Step 3 — Root cause analysis.** Each CARC code gets its own rule function:
- CARC 29 (timely filing): compares service date vs payer received date, checks insurance type for the applicable limit (Medicare = 365 days, commercial varies), looks for delay reason codes and replacement claim indicators.
- CARC 16/252 (missing info): checks remark codes (N20, M127) and scans for empty required fields.
- CARC 50 (medical necessity): matches procedure against known supportable diagnoses, also checks for prior auth as a fallback.
- CARC 18/97 (duplicate): builds a claim signature (payer + CPT + date + NPI + member ID) and looks for exact matches in history.
- CARC 197 (auth): just checks whether an auth number was on the original 837.
- CARC 4 (coding): flags missing modifier 25 on E&M codes, routes others to coder review.

**Step 4 — Batch clustering.** Groups all denied claims by payer + CARC + procedure. Each cluster gets an estimated recoverable dollar amount so billing teams can prioritize. The cluster with the highest expected recovery comes first.

---

## Design decisions

I went with rule-based logic for the denial reasoning rather than feeding raw claim data straight to an LLM. The main reason is auditability — billing teams need to see exactly why a verdict was reached, not just a summary. The rules produce concrete field-level evidence that can go directly into an appeal letter.

That said, I kept the LLM prompt in `prompts/claim_analysis.yaml` because in production you'd want it for the parts that don't lend themselves to rules: drafting appeal language, reasoning over payer policy documents, and handling edge cases the rules haven't seen. The prompt and the rule engine produce the same JSON schema, so you can swap them or run both and compare.

For claim similarity, I used weighted feature matching rather than embeddings. Claim data is sparse and structured — you care a lot about exact payer/procedure matches, less about whether the clinical notes sound alike. The weights reflect that: payer and procedure each get 25%, billing NPI gets 15%, diagnosis overlap is Jaccard-scored, and place of service / auth presence are minor tiebreakers. I tuned these by thinking through what a billing analyst actually looks at when pulling comps.

---

## Synthetic dataset

`generate_synthetic_data.py` creates 60 claims total:

- The 4 exact sample claims from the assignment (used as regression fixtures)
- 24 paid historical claims across payers and procedures
- 32 denied claims covering timely filing, missing info, medical necessity, auth, duplicate, coding, and non-covered scenarios

Each denied claim has an `expected.recoverability_verdict` label so the evaluation step can check accuracy. The generator makes actual duplicate claims by copying the payer/procedure/date/NPI/member signature from an existing paid claim — that's what lets the duplicate-detection rule work correctly.

---

## Evaluation

The eval step checks predicted verdicts against the synthetic labels. On the current ruleset: **100% accuracy, avg confidence 0.74** across 36 labeled denied claims.

That's an encouraging number but shouldn't be taken at face value — I wrote both the rules and the labels, so they're naturally aligned. In a real setting you'd want:
- Expert-labeled appeal outcomes from an actual billing team
- Held-out payer/procedure combinations the rules were never tuned on
- Calibration checks (does a 0.8 confidence actually recover ~80% of the time?)
- Some way to measure evidence quality, not just verdict correctness

---

## Limitations

- Filing limits and medical necessity policies are simplified. Real payers have quirks not captured here.
- The system works on pre-parsed JSON. It doesn't ingest raw X12 EDI files.
- The synthetic dataset is small (60 claims). Pattern matching needs more history to be statistically meaningful.
- Auth lookups only check the 837 fields — a real system would query the precert database.

---

## Repo layout

```
main.py                    pipeline, matching, clustering, eval
generate_synthetic_data.py creates the test dataset
data/
  synthetic_claims.json    generated test claims (60 total)
  835_Attributes.csv       835 schema reference from assignment
  837_Attributes.csv       837 schema reference from assignment
prompts/
  claim_analysis.yaml      LLM prompt (optional, same JSON output contract)
outputs/
  claim_analyses.json      per-claim results
  denial_clusters.json     batch clusters ranked by recovery potential
  evaluation.json          accuracy stats
```
