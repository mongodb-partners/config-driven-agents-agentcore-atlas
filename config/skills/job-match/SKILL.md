---
name: job-match
description: Semantic job matching against open roles, plus grounded interview preparation.
---

# Job Match Skill

Field-level schemas are in `references/collections-schema.md`. Read it with
`read_skill_resource` when you need exact field names.

---

## Workflow A — Match roles to a candidate

**A1. Build the profile.**
- If the message contains CV or resume text, use it directly.
- If it gives a `candidateId`, call `mongodb_find` on `candidates` with
  `{"candidateId": "<id>"}`.
- If neither, ask the candidate to paste their skills, current role, and years of
  experience, then stop.

**A2. Search.** Call `vector_search`:
```
collection:  jobs
query_text:  "<current role>, <seniority>, <years>y experience. Skills: <comma list>.
              Looking for: <stated preference or target role>."
filter:      {"location": "<city>"}   # only if the candidate stated a location constraint
limit:       5
```

**A3. Score each result honestly.** For every returned job, intersect the candidate's skills
with `requiredSkills`. A job whose `core` requirements are unmet is a *partial* fit — label
it as such rather than dropping or inflating it.

**A4. Present** at most 5, best first:

> **<title>** — <company>, <location> · `<jobId>`
> Fits because: <2–3 matched skills>. Gap: <unmet core requirements, or "none">.

Then one line on what the candidate should strengthen before applying.

---

## Workflow B — Interview prep

**B1. Resolve the role.** By `jobId` if given, otherwise `vector_search` on `jobs` with the
role and company named in the message (limit 1). If you cannot pin a specific role, ask
which one and stop.

**B2. Generate questions** grounded in that job's retrieved `requiredSkills` and
`responsibilities`, grouped:
- **Technical** — one per core required skill.
- **Experience** — one per responsibility, phrased as "tell me about a time…".
- **Role fit** — drawn from the job's `description`.

For each question add a one-line note on what a strong answer covers. Base that note on the
retrieved role data, not on generic interview advice.

---

## If nothing is found

Say no open roles matched, name which part of the criteria was most limiting, and suggest
one specific way to broaden. Never present a fabricated opening.
