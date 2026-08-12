---
name: company-intel
description: Employer research — culture, engineering practices, stage, and sentiment.
---

# Company Intel Skill

Field-level schemas are in `references/collections-schema.md`. Read it with
`read_skill_resource` when you need exact field names.

---

## Workflow A — Profile one company

**A1. Retrieve.** Call `vector_search`:
```
collection:  companies
query_text:  "<company name> <the dimension asked about, e.g. engineering culture>"
limit:       3
```

**A2. Confirm identity.** If no result's `name` is the company the candidate named, stop and
say the company is not in the dataset. Do not answer about it from your own knowledge —
a plausible-sounding but ungrounded company profile is the worst failure mode here.

**A3. Present** what the data supports:
- **Snapshot** — `industry`, `size`, `stage`, `founded`, `headquarters`
- **Engineering** — `techStack`, `engineeringPractices`
- **Culture** — `culture` signals, `benefits`
- **Sentiment** — `sentiment.score` with `sentiment.reviewCount` and `asOf`, then the
  `sentiment.themes`, positive *and* negative

Give the trade-offs explicitly. A candidate choosing an employer needs the downsides.

---

## Workflow B — Compare companies

**B1.** One `vector_search` per company named (limit 2 each).

**B2.** Any company not found is reported as not found — do not quietly drop it from the
comparison or the candidate will read the table as complete.

**B3.** Present a table, companies as columns, and pick rows from the dimensions the
candidate actually asked about (default: size, stage, tech stack, culture, sentiment).
Close with 2–3 lines on which trade-offs matter for which kind of candidate — framed as
"if you want X, then Y" rather than declaring a winner.

---

## Hard rule

You have exactly one source: the `companies` collection. If it does not have the company,
you do not know the company. Say so.
