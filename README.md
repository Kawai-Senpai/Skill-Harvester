# Agent Skill Harvester

This is a practical collector for public Agent Skills. It is built for one job: pull skill folders from multiple places, normalize them into one vault, and leave behind indexes you can actually use.

If your current plan is manually clicking through `skills.sh` pages and copy-pasting folders one by one, that plan is bad.

## What it supports

- `skills-api` compatible registries via `--skills-api-base`
- `skills.sh` homepage and trending pages as HTML discovery fallbacks
- GitHub topic discovery for skill-related repos
- explicit seed repos from flags or a text file
- validation of `name` and `description` frontmatter in `SKILL.md`
- JSON, JSONL, and CSV indexes for the collected vault

## What it does not pretend to do

- It does not guarantee perfect coverage of the entire internet.
- It does not magically bypass GitHub rate limits.
- It does not guarantee every discovered `SKILL.md` is safe.
- It does not solve licensing for you.

That last one matters. Collecting is easy. Redistributing a giant bundle without checking licenses is how people end up doing dumb things at scale.

## Fast start

Use the bundled seed list and a GitHub token:

```bash
export GITHUB_TOKEN=ghp_your_token_here
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --seed-file assets/seed-repos.txt
````

If you have a `skills-api` compatible endpoint, use it. That is the cleanest route to broad registry coverage.

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --skills-api-base https://your-registry.example.com/api \
  --seed-file assets/seed-repos.txt \
  --gh-token "$GITHUB_TOKEN"
```

## Good example commands

Collect from registry + topics + seeds:

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --skills-api-base https://your-registry.example.com/api \
  --seed-file assets/seed-repos.txt \
  --topic agent-skills \
  --topic claude-code-skills \
  --topic claude-skills \
  --topic copilot-coding-agent \
  --workers 8 \
  --gh-token "$GITHUB_TOKEN"
```

Limit the blast radius while testing:

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault-test \
  --seed-file assets/seed-repos.txt \
  --max-repos 20 \
  --workers 4 \
  --gh-token "$GITHUB_TOKEN"
```

Only keep skills explicitly named by the registry for each repo:

```bash
python scripts/collect_skills.py collect \
  --out ./skill-vault \
  --skills-api-base https://your-registry.example.com/api \
  --registry-only \
  --gh-token "$GITHUB_TOKEN"
```

Rebuild indexes later:

```bash
python scripts/collect_skills.py reindex --out ./skill-vault
```

## Output layout

```text
skill-vault/
├── repos/
│   ├── anthropics__skills/
│   │   ├── docx/
│   │   ├── pdf/
│   │   └── ...
│   └── vercel-labs__agent-skills/
│       ├── web-design-guidelines/
│       └── ...
├── errors.json
├── index.csv
├── index.json
├── index.jsonl
└── summary.json
```

Each copied skill folder also gets a `.collector-manifest.json` file with repo/source metadata.

## How discovery works

### 1. Registry discovery

If you pass `--skills-api-base`, the script uses a `skills-api` style endpoint and paginates through skills. This is the best option for large-scale collection.

### 2. `skills.sh` fallback discovery

The script scrapes `https://skills.sh/` and `https://skills.sh/trending` for public skill routes. This is useful, but it is not a full registry export.

### 3. GitHub topic discovery

The script queries skill-adjacent topics to catch repos that are not in your registry results or seed file.

### 4. Seed repositories

Use `--seed-repo owner/repo` or `--seed-file repos.txt` for known good sources.

## How validation works

A folder is treated as a valid skill only if:

* it contains `SKILL.md`
* frontmatter has `name`
* frontmatter has `description`
* `name` matches the parent directory name
* `name` matches the basic Agent Skills naming pattern

This is intentionally strict. A sloppy collector becomes a junk drawer.

## Operational advice

* Use a GitHub token. Unauthenticated API usage is a self-inflicted wound.
* Start with `--max-repos 20` to verify output structure.
* Keep the raw vault separate from any curated, deduped, or redistributed pack.
* Review `errors.json` instead of pretending every failure is fine.
* Review licenses before publishing a bundle.
* Review downloaded `scripts/` content before running anything.

## Extending it

Obvious next upgrades if you want to make this nastier in a good way:

* add content-hash dedupe across repos
* store repo stars, updated date, and topic metadata in the index
* support GitLab archives directly
* support code search for `filename:SKILL.md`
* add SQLite instead of just JSON and CSV
* add allowlists and blocklists for owners and licenses

## License

MIT

````