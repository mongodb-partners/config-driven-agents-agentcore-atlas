# Collections — company-intel

**Read-only.** One collection. If a company is not in it, you do not know that company.

---

## `companies` — vector index `companies_vector_index`

```json
{
  "companyId": "co-acme-robotics",
  "name": "Acme Robotics",
  "industry": "Robotics / Warehouse automation",
  "size": "450",
  "stage": "Series C",
  "founded": 2016,
  "headquarters": "London, UK",
  "funding": { "totalRaisedUsd": 180000000, "lastRound": "Series C", "lastRoundYear": 2025 },
  "techStack": ["python", "pytorch", "ros2", "kubernetes", "mongodb"],
  "engineeringPractices": [
    "Trunk-based development, deploys ~20x/day",
    "On-call rotation with a 1-week cadence"
  ],
  "culture": [
    "Written-first — proposals circulate as docs before meetings",
    "Hardware release cycles create real crunch before customer pilots"
  ],
  "benefits": ["private health", "learning budget £2k/yr", "hybrid 3 days on-site"],
  "sentiment": {
    "score": 3.9,
    "reviewCount": 212,
    "asOf": "2026-03-01",
    "themes": {
      "positive": ["strong mentorship", "genuinely hard technical problems"],
      "negative": ["compensation lags London market", "pre-launch crunch is real"]
    }
  }
}
```

| Field | Notes |
|---|---|
| `companyId` | Cite this, never `_id` |
| `size` | Headcount as a string — may be a band like `"200-500"` |
| `stage` | `seed` \| `Series A/B/C` \| `public` \| `bootstrapped` |
| `sentiment.score` | Out of 5. **Always** report `reviewCount` and `asOf` with it — a 4.8 from 6 reviews is not a 4.8 from 600 |
| `sentiment.themes.negative` | **Never omit these.** A one-sided profile is the failure mode this agent exists to avoid |
| `culture` | Written to include the trade-offs, not just the perks — present both |
