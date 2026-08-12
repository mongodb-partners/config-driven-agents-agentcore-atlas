# Collections — career-coach

All three collections are **read-only**. Every one carries a 1024-dim `embedding` field;
never project it into your context and never mention it.

---

## `career_paths` — vector index `career_paths_vector_index`

```json
{
  "pathId": "path-backend-to-ml-engineer",
  "targetRole": "Machine Learning Engineer",
  "fromRoles": ["Backend Engineer", "Full-Stack Engineer"],
  "seniority": "mid",
  "description": "How to move from a backend role into ML engineering.",
  "requiredSkills": [
    { "skillId": "skill-python", "name": "python", "importance": "core",
      "note": "Primary language for ML tooling" }
  ],
  "milestones": [
    { "order": 1, "title": "Foundations — Python for ML",
      "skills": ["skill-python"], "estimatedWeeks": 8 }
  ]
}
```

| Field | Notes |
|---|---|
| `pathId` | Cite this, never `_id` |
| `seniority` | `junior` \| `mid` \| `senior` \| `staff` — level of the **target** role |
| `requiredSkills[].importance` | `core` \| `important` \| `nice` — core gaps lead your answer |
| `milestones` | Ordered by `order`; `estimatedWeeks` is the phase budget |

---

## `skills_taxonomy` — vector index `skills_taxonomy_vector_index`

```json
{
  "skillId": "skill-python",
  "name": "python",
  "category": "language",
  "description": "General-purpose language, dominant in ML and data tooling.",
  "relatedSkills": ["skill-pandas", "skill-numpy"],
  "learningResources": [
    { "title": "Python for Data Analysis", "type": "book", "estimatedHours": 30 }
  ]
}
```

`learningResources[].type` is `course` \| `book` \| `docs` \| `project`. Present only what
is here — never add a course you know of.

---

## `compensation_data` — vector index `compensation_data_vector_index`

```json
{
  "benchmarkId": "comp-ml-engineer-senior-london",
  "role": "Machine Learning Engineer",
  "seniority": "senior",
  "location": "London, UK",
  "currency": "GBP",
  "base":  { "p25": 85000,  "p50": 100000, "p75": 118000 },
  "total": { "p25": 95000,  "p50": 118000, "p75": 145000 },
  "benefits": ["private health", "10% pension match", "equity refresh"],
  "sampleSize": 142,
  "asOf": "2026-01-01"
}
```

| Field | Notes |
|---|---|
| `benchmarkId` | Cite this, never `_id` |
| `seniority` | `junior` \| `mid` \| `senior` \| `staff` — safe to use as a `$vectorSearch` pre-filter |
| `base` / `total` | Percentiles. **Always present as a range**, never a single figure |
| `currency` | Always state it — bands span regions |
| `asOf` | Always state it — a stale band presented as current is misleading |
