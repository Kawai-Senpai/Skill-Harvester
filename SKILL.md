---
name: agent-skill-harvester
description: Discover, download, validate, and index public Agent Skill folders from skills.sh-compatible registries, GitHub topic searches, and seed repositories. Use when building a local skill vault, backing up skills, or collecting many skill folders automatically.
license: MIT
compatibility: Python 3.10+ with network access. Optional GITHUB_TOKEN recommended for higher GitHub API limits.
metadata:
  author: OpenAI
  version: "0.1.0"
---

# Agent Skill Harvester

This skill builds and maintains a local vault of public Agent Skill folders.

## When to use it

Use this skill when you want to:

- collect many public `SKILL.md` folders into one local directory
- back up skills from `skills.sh`-style registries and GitHub repos
- build an offline catalog of skills with JSON and CSV indexes
- validate that collected skills roughly match the Agent Skills spec

## What it does

The harvester script combines several discovery paths:

1. A `skills-api` compatible registry if you provide `--skills-api-base`
2. `skills.sh` homepage and trending pages as an HTML fallback for popular skills
3. GitHub topic search for repos tagged with skill-related topics
4. explicit seed repositories from a CLI flag or a text file

For each discovered repo, it downloads the repository archive from GitHub, finds every `SKILL.md`, validates the frontmatter, and copies valid skill folders into a local vault.

## Files in this skill

- `scripts/collect_skills.py` - the harvester CLI
- `assets/seed-repos.txt` - starter list of strong seed repos
- `README.md` - practical setup and usage notes

## Run it

Basic run:

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --seed-file assets/seed-repos.txt \
  --gh-token "$GITHUB_TOKEN"
````

With a registry API:

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --skills-api-base https://your-registry.example.com/api \
  --seed-file assets/seed-repos.txt \
  --gh-token "$GITHUB_TOKEN"
```

Rebuild indexes later:

```bash
python scripts/collect_skills.py reindex --out ./skill-vault
```

## Output layout

The script writes:

* `repos/<owner>__<repo>/<skill-name>/...`
* `index.json`
* `index.jsonl`
* `index.csv`
* `errors.json`
* `summary.json`

## Rules and cautions

* Do not blindly execute downloaded scripts from collected skills.
* Review licenses before redistributing a large bundle.
* Use a GitHub token unless you enjoy smashing into rate limits like a clown.
* `skills.sh` HTML scraping only sees public browse pages. For a full registry, prefer a `skills-api` compatible endpoint.

````