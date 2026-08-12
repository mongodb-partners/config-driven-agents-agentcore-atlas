# Collections — job-match

Both collections are **read-only**. `jobs` carries a 1024-dim `embedding`; never project it
into your context and never mention it.

---

## `jobs` — vector index `jobs_vector_index`

```json
{
  "jobId": "job-ml-eng-acme-001",
  "title": "Machine Learning Engineer",
  "company": "Acme Robotics",
  "companyId": "co-acme-robotics",
  "location": "London, UK",
  "remote": "hybrid",
  "seniority": "mid",
  "employmentType": "full-time",
  "description": "Build and ship models for warehouse automation.",
  "responsibilities": [
    "Own model training pipelines end to end",
    "Partner with robotics engineers on deployment"
  ],
  "requiredSkills": [
    { "skillId": "skill-python", "name": "python", "importance": "core" },
    { "skillId": "skill-pytorch", "name": "pytorch", "importance": "important" }
  ],
  "yearsExperience": { "min": 3, "max": 6 },
  "postedAt": "2026-06-01"
}
```

| Field | Notes |
|---|---|
| `jobId` | Cite this, never `_id` |
| `companyId` | Hand this to the candidate if they want employer research — `company-intel` uses it |
| `remote` | `onsite` \| `hybrid` \| `remote` |
| `seniority` | `junior` \| `mid` \| `senior` \| `staff` — safe as a `$vectorSearch` pre-filter |
| `location` | Safe as a pre-filter, but exact-match — only filter when the candidate named a place |
| `requiredSkills[].importance` | `core` \| `important` \| `nice`. Unmet **core** = partial fit, say so |
| `responsibilities` | The source for "tell me about a time…" interview questions |

---

## `candidates` — no vector index, query by `candidateId`

```json
{
  "candidateId": "cand-0001",
  "name": "Priya Raman",
  "email": "priya@example.com",
  "profile": {
    "currentRole": "Backend Engineer",
    "seniority": "mid",
    "yearsExperience": 4,
    "location": "London, UK",
    "skills": ["python", "postgres", "aws", "docker"],
    "targetRole": "Machine Learning Engineer"
  }
}
```

Look this up with `mongodb_find` and `{"candidateId": "<id>"}`.
Never surface `email` in an answer.
