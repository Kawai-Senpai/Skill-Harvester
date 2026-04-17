#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

USER_AGENT = "agent-skill-harvester/0.1"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)
SKILLS_SH_LINK_RE = re.compile(
    r'(?:https?://skills\\.sh|href=")/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)'
)


@dataclass
class RepoRef:
    owner: str
    repo: str
    html_url: str
    topics: List[str] = field(default_factory=list)
    discovered_from: Set[str] = field(default_factory=set)
    registry_skill_ids: Set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass
class SkillRecord:
    owner: str
    repo: str
    repo_url: str
    local_path: str
    skill_dir_name: str
    declared_name: str
    description: str
    license: str
    compatibility: str
    metadata: Dict[str, Any]
    discovered_from: List[str]
    registry_skill_ids: List[str]
    skill_md_sha256: str


@dataclass
class RepoProcessResult:
    repo: str
    copied: int = 0
    skipped: int = 0
    invalid: int = 0
    error: str = ""
    records: List[SkillRecord] = field(default_factory=list)


class HttpClient:
    def __init__(
        self,
        token: Optional[str] = None,
        timeout: int = 30,
        retries: int = 3,
        min_delay: float = 0.25,
    ) -> None:
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.min_delay = min_delay
        self._lock = threading.Lock()
        self._last_request = 0.0

    def _headers(self, accept: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept or "application/json, text/plain;q=0.9, text/html;q=0.8, */*;q=0.7",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers

    def _rate_limit_wait(self, headers: Dict[str, str]) -> Optional[float]:
        retry_after = headers.get("Retry-After")
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")
        if remaining == "0" and reset and reset.isdigit():
            wait_for = int(reset) - int(time.time()) + 1
            return float(max(wait_for, 1))
        return None

    def _paced_open(self, req: urllib.request.Request) -> bytes:
        for attempt in range(1, self.retries + 1):
            try:
                with self._lock:
                    now = time.time()
                    sleep_for = self.min_delay - (now - self._last_request)
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    self._last_request = time.time()
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                wait_for = self._rate_limit_wait(exc.headers)
                if wait_for is not None and attempt < self.retries:
                    print(
                        f"[rate-limit] HTTP {exc.code} for {req.full_url} -> sleeping {int(wait_for)}s",
                        file=sys.stderr,
                    )
                    time.sleep(wait_for)
                    continue
                if exc.code in {403, 429} and attempt < self.retries:
                    time.sleep(attempt * 2)
                    continue
                if attempt < self.retries and 500 <= exc.code < 600:
                    time.sleep(attempt * 1.5)
                    continue
                raise
            except urllib.error.URLError:
                if attempt < self.retries:
                    time.sleep(attempt * 1.5)
                    continue
                raise
        raise RuntimeError("unreachable")

    def get_bytes(self, url: str, accept: Optional[str] = None) -> bytes:
        req = urllib.request.Request(url, headers=self._headers(accept))
        return self._paced_open(req)

    def get_text(self, url: str, accept: Optional[str] = None) -> str:
        data = self.get_bytes(url, accept=accept or "text/html, text/plain;q=0.9, */*;q=0.7")
        return data.decode("utf-8", errors="replace")

    def get_json(self, url: str) -> Any:
        data = self.get_bytes(url, accept="application/vnd.github+json, application/json")
        return json.loads(data.decode("utf-8", errors="replace"))


def debug(msg: str, enabled: bool) -> None:
    if enabled:
        print(msg, file=sys.stderr)


def dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def merge_repo(target: Dict[str, RepoRef], incoming: RepoRef) -> None:
    existing = target.get(incoming.key)
    if existing is None:
        target[incoming.key] = incoming
        return
    existing.discovered_from.update(incoming.discovered_from)
    existing.registry_skill_ids.update(incoming.registry_skill_ids)
    existing.topics = dedupe_keep_order(existing.topics + incoming.topics)


def manual_frontmatter_parse(raw: str) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    current_parent: Optional[str] = None
    nested: Dict[str, Any] = {}
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  ") or line.startswith("\t"):
            if current_parent == "metadata" and ":" in line:
                key, value = line.strip().split(":", 1)
                nested[key.strip()] = value.strip().strip('"\'')
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not value:
            if key == "metadata":
                current_parent = key
                data[key] = nested
            else:
                data[key] = ""
            continue
        current_parent = None
        data[key] = value.strip('"\'')
    return data


def parse_frontmatter(skill_md_path: Path) -> Tuple[Dict[str, Any], str]:
    text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    match = FRONTMATTER_RE.search(text)
    if not match:
        return {}, text
    raw = match.group(1)
    parsed: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore

        candidate = yaml.safe_load(raw)
        if isinstance(candidate, dict):
            parsed = candidate
        else:
            parsed = {}
    except Exception:
        parsed = manual_frontmatter_parse(raw)
    return parsed, text


def validate_skill_dir(skill_dir: Path) -> Tuple[bool, str, Dict[str, Any], str]:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False, "missing SKILL.md", {}, ""
    frontmatter, text = parse_frontmatter(skill_md)
    name = str(frontmatter.get("name", "")).strip()
    description = str(frontmatter.get("description", "")).strip()
    if not name or not description:
        return False, "missing required name/description", frontmatter, text
    if not NAME_RE.fullmatch(name):
        return False, f"invalid name '{name}'", frontmatter, text
    if skill_dir.name != name:
        return False, f"parent directory '{skill_dir.name}' != declared name '{name}'", frontmatter, text
    if not (1 <= len(description) <= 1024):
        return False, "description length out of spec", frontmatter, text
    return True, "", frontmatter, text


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def parse_owner_repo(repo_ref: str) -> Tuple[str, str]:
    repo_ref = repo_ref.strip()
    if repo_ref.startswith("https://github.com/"):
        path = urllib.parse.urlparse(repo_ref).path.strip("/")
        parts = path.split("/")
        if len(parts) >= 2:
            return parts[0], parts[1].removesuffix(".git")
    if repo_ref.count("/") == 1:
        owner, repo = repo_ref.split("/", 1)
        return owner.strip(), repo.strip().removesuffix(".git")
    raise ValueError(f"Expected owner/repo or GitHub URL, got: {repo_ref}")


def load_seed_repos(seed_repos: List[str], seed_file: Optional[str]) -> List[str]:
    items = list(seed_repos)
    if seed_file:
        for line in Path(seed_file).read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(line)
    return dedupe_keep_order(items)


def discover_from_seed_repos(seed_repos: List[str]) -> Dict[str, RepoRef]:
    repos: Dict[str, RepoRef] = {}
    for item in seed_repos:
        try:
            owner, repo = parse_owner_repo(item)
        except ValueError:
            continue
        merge_repo(
            repos,
            RepoRef(
                owner=owner,
                repo=repo,
                html_url=f"https://github.com/{owner}/{repo}",
                discovered_from={"seed"},
            ),
        )
    return repos


def discover_from_skills_api(
    client: HttpClient,
    base_url: str,
    debug_enabled: bool,
    page_size: int = 100,
    max_pages: int = 0,
) -> Dict[str, RepoRef]:
    repos: Dict[str, RepoRef] = {}
    page = 1
    while True:
        url = f"{base_url.rstrip('/')}/skills?page={page}&pageSize={page_size}&sortBy=installs&sortOrder=desc"
        debug(f"[skills-api] {url}", debug_enabled)
        data = client.get_json(url)
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("skills") or data.get("items") or data.get("data") or []
        if not items:
            break
        for item in items:
            if not isinstance(item, dict):
                continue
            owner = str(item.get("owner") or "").strip()
            repo = str(item.get("repo") or "").strip()
            source = str(item.get("source") or "").strip()
            if (not owner or not repo) and "/" in source:
                owner, repo = source.split("/", 1)
            skill_id = str(item.get("skillId") or item.get("id") or item.get("name") or "").strip()
            if not owner or not repo:
                continue
            ref = RepoRef(
                owner=owner,
                repo=repo,
                html_url=f"https://github.com/{owner}/{repo}",
                discovered_from={f"skills-api:{base_url.rstrip('/')}"},
                registry_skill_ids={skill_id} if skill_id else set(),
            )
            merge_repo(repos, ref)
        page += 1
        if max_pages and page > max_pages:
            break
    return repos


def discover_from_skills_sh_html(client: HttpClient, urls: List[str], debug_enabled: bool) -> Dict[str, RepoRef]:
    repos: Dict[str, RepoRef] = {}
    for url in urls:
        debug(f"[skills.sh-html] {url}", debug_enabled)
        try:
            html = client.get_text(url, accept="text/html, text/plain;q=0.9, */*;q=0.7")
        except Exception:
            continue
        for match in SKILLS_SH_LINK_RE.finditer(html):
            owner, repo, skill_id = match.groups()
            if owner in {"docs", "trending", "official", "audits"}:
                continue
            merge_repo(
                repos,
                RepoRef(
                    owner=owner,
                    repo=repo,
                    html_url=f"https://github.com/{owner}/{repo}",
                    discovered_from={f"skills.sh-html:{url}"},
                    registry_skill_ids={skill_id},
                ),
            )
    return repos


def discover_from_github_topics(
    client: HttpClient,
    topics: List[str],
    pages_per_topic: int,
    debug_enabled: bool,
) -> Dict[str, RepoRef]:
    repos: Dict[str, RepoRef] = {}
    for topic in topics:
        for page in range(1, pages_per_topic + 1):
            q = urllib.parse.quote(f"topic:{topic}")
            url = (
                "https://api.github.com/search/repositories"
                f"?q={q}&sort=stars&order=desc&per_page=100&page={page}"
            )
            debug(f"[github-topics] {url}", debug_enabled)
            try:
                data = client.get_json(url)
            except Exception:
                break
            items = data.get("items", []) if isinstance(data, dict) else []
            if not items:
                break
            for item in items:
                full_name = str(item.get("full_name") or "")
                if "/" not in full_name:
                    continue
                owner, repo = full_name.split("/", 1)
                merge_repo(
                    repos,
                    RepoRef(
                        owner=owner,
                        repo=repo,
                        html_url=str(item.get("html_url") or f"https://github.com/{full_name}"),
                        topics=[topic],
                        discovered_from={f"github-topic:{topic}"},
                    ),
                )
            if len(items) < 100:
                break
    return repos


def get_repo_info(client: HttpClient, owner: str, repo: str) -> Dict[str, Any]:
    return client.get_json(f"https://api.github.com/repos/{owner}/{repo}")


def download_github_zip(client: HttpClient, owner: str, repo: str, ref: Optional[str], debug_enabled: bool) -> bytes:
    if ref:
        encoded_ref = urllib.parse.quote(ref, safe="")
        url = f"https://api.github.com/repos/{owner}/{repo}/zipball/{encoded_ref}"
    else:
        url = f"https://api.github.com/repos/{owner}/{repo}/zipball"
    debug(f"[zip] {url}", debug_enabled)
    return client.get_bytes(url, accept="application/vnd.github+json, application/zip, application/octet-stream")


def extract_zip_bytes(data: bytes, dest: Path) -> Path:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)
    roots = [p for p in dest.iterdir() if p.is_dir()]
    if len(roots) == 1:
        return roots[0]
    return dest


def git_clone(url: str, dest: Path, debug_enabled: bool) -> None:
    debug(f"[git-clone] {url}", debug_enabled)
    subprocess.run(
        ["git", "clone", "--depth", "1", url, str(dest)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def find_skill_dirs(root: Path) -> Iterator[Path]:
    ignore_parts = {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__"}
    for skill_md in root.rglob("SKILL.md"):
        if any(part in ignore_parts for part in skill_md.parts):
            continue
        yield skill_md.parent


def safe_copytree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def write_index_files(out_dir: Path, records: List[SkillRecord], errors: List[Dict[str, Any]]) -> None:
    records_payload = [record.__dict__ for record in records]
    write_json(out_dir / "index.json", records_payload)
    with (out_dir / "index.jsonl").open("w", encoding="utf-8") as f:
        for item in records_payload:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    with (out_dir / "index.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "owner",
                "repo",
                "repo_url",
                "local_path",
                "skill_dir_name",
                "declared_name",
                "description",
                "license",
                "compatibility",
                "skill_md_sha256",
                "discovered_from",
                "registry_skill_ids",
                "metadata",
            ],
        )
        writer.writeheader()
        for record in records_payload:
            row = dict(record)
            row["discovered_from"] = "; ".join(record["discovered_from"])
            row["registry_skill_ids"] = "; ".join(record["registry_skill_ids"])
            row["metadata"] = json.dumps(record["metadata"], ensure_ascii=False)
            writer.writerow(row)
    write_json(out_dir / "errors.json", errors)


def process_github_repo(
    client: HttpClient,
    repo_ref: RepoRef,
    out_dir: Path,
    refresh_existing: bool,
    registry_only: bool,
    debug_enabled: bool,
) -> RepoProcessResult:
    result = RepoProcessResult(repo=repo_ref.key)
    repo_base = out_dir / "repos" / f"{repo_ref.owner}__{repo_ref.repo}"
    if repo_base.exists() and not refresh_existing:
        result.skipped += 1
        return result

    with tempfile.TemporaryDirectory(prefix="skill-harvest-") as td:
        temp_root = Path(td)
        try:
            info = get_repo_info(client, repo_ref.owner, repo_ref.repo)
            ref = str(info.get("default_branch") or "") or None
            zip_bytes = download_github_zip(client, repo_ref.owner, repo_ref.repo, ref, debug_enabled)
            checkout = extract_zip_bytes(zip_bytes, temp_root / "archive")
        except Exception as exc:
            result.error = f"download failed: {exc}"
            return result

        matched_registry_ids = {s.lower() for s in repo_ref.registry_skill_ids if s}
        seen_names: Set[str] = set()
        for skill_dir in find_skill_dirs(checkout):
            valid, reason, frontmatter, text = validate_skill_dir(skill_dir)
            if not valid:
                result.invalid += 1
                continue
            declared_name = str(frontmatter.get("name", "")).strip()
            if registry_only and matched_registry_ids and declared_name.lower() not in matched_registry_ids:
                result.skipped += 1
                continue
            if declared_name in seen_names:
                result.skipped += 1
                continue
            seen_names.add(declared_name)
            dest = repo_base / declared_name
            if dest.exists() and not refresh_existing:
                result.skipped += 1
                continue
            safe_copytree(skill_dir, dest)
            manifest = {
                "owner": repo_ref.owner,
                "repo": repo_ref.repo,
                "repo_url": repo_ref.html_url,
                "declared_name": declared_name,
                "description": str(frontmatter.get("description", "")).strip(),
                "license": str(frontmatter.get("license", "")).strip(),
                "compatibility": str(frontmatter.get("compatibility", "")).strip(),
                "metadata": frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {},
                "discovered_from": sorted(repo_ref.discovered_from),
                "registry_skill_ids": sorted(repo_ref.registry_skill_ids),
                "skill_md_sha256": sha256_text(text),
            }
            write_json(dest / ".collector-manifest.json", manifest)
            result.records.append(
                SkillRecord(
                    owner=repo_ref.owner,
                    repo=repo_ref.repo,
                    repo_url=repo_ref.html_url,
                    local_path=str(dest.relative_to(out_dir)),
                    skill_dir_name=skill_dir.name,
                    declared_name=declared_name,
                    description=str(frontmatter.get("description", "")).strip(),
                    license=str(frontmatter.get("license", "")).strip(),
                    compatibility=str(frontmatter.get("compatibility", "")).strip(),
                    metadata=frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {},
                    discovered_from=sorted(repo_ref.discovered_from),
                    registry_skill_ids=sorted(repo_ref.registry_skill_ids),
                    skill_md_sha256=sha256_text(text),
                )
            )
            result.copied += 1
    return result


def collect_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    (out_dir / "repos").mkdir(parents=True, exist_ok=True)

    client = HttpClient(
        token=args.gh_token or os.getenv("GITHUB_TOKEN"),
        timeout=args.timeout,
        retries=args.retries,
        min_delay=args.min_delay,
    )

    all_repos: Dict[str, RepoRef] = {}

    if args.skills_api_base:
        try:
            api_repos = discover_from_skills_api(
                client,
                args.skills_api_base,
                debug_enabled=args.debug,
                page_size=args.api_page_size,
                max_pages=args.api_max_pages,
            )
            for repo in api_repos.values():
                merge_repo(all_repos, repo)
        except Exception as exc:
            print(f"[warn] skills API discovery failed: {exc}", file=sys.stderr)

    if args.skills_sh_html:
        html_urls = ["https://skills.sh/", "https://skills.sh/trending"]
        html_repos = discover_from_skills_sh_html(client, html_urls, debug_enabled=args.debug)
        for repo in html_repos.values():
            merge_repo(all_repos, repo)

    topic_repos = discover_from_github_topics(
        client,
        topics=args.topic,
        pages_per_topic=args.topic_pages,
        debug_enabled=args.debug,
    )
    for repo in topic_repos.values():
        merge_repo(all_repos, repo)

    seed_repos = load_seed_repos(args.seed_repo, args.seed_file)
    for repo in discover_from_seed_repos(seed_repos).values():
        merge_repo(all_repos, repo)

    repos_list = list(all_repos.values())
    repos_list.sort(key=lambda r: (r.owner.lower(), r.repo.lower()))
    if args.max_repos:
        repos_list = repos_list[: args.max_repos]

    print(f"Discovered {len(repos_list)} repositories to inspect.")
    records: List[SkillRecord] = []
    errors: List[Dict[str, Any]] = []

    with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                process_github_repo,
                client,
                repo,
                out_dir,
                args.refresh_existing,
                args.registry_only,
                args.debug,
            ): repo
            for repo in repos_list
        }
        for idx, future in enumerate(cf.as_completed(futures), start=1):
            repo = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                errors.append({"repo": repo.key, "error": str(exc)})
                print(f"[{idx}/{len(futures)}] {repo.key} -> ERROR: {exc}")
                continue
            if result.error:
                errors.append({"repo": repo.key, "error": result.error})
                print(f"[{idx}/{len(futures)}] {repo.key} -> ERROR: {result.error}")
                continue
            records.extend(result.records)
            print(
                f"[{idx}/{len(futures)}] {repo.key} -> copied={result.copied} skipped={result.skipped} invalid={result.invalid}"
            )

    write_index_files(out_dir, records, errors)
    summary = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "repositories_considered": len(repos_list),
        "skills_copied": len(records),
        "repos_with_errors": len(errors),
        "output": str(out_dir),
        "index_json": str((out_dir / "index.json").resolve()),
        "index_csv": str((out_dir / "index.csv").resolve()),
    }
    write_json(out_dir / "summary.json", summary)

    print("\nDone.")
    print(json.dumps(summary, indent=2))
    return 0


def reindex_command(args: argparse.Namespace) -> int:
    out_dir = Path(args.out).resolve()
    records: List[SkillRecord] = []
    errors: List[Dict[str, Any]] = []
    repos_dir = out_dir / "repos"
    if not repos_dir.exists():
        print(f"No repos directory found at {repos_dir}", file=sys.stderr)
        return 1

    for skill_md in repos_dir.rglob("SKILL.md"):
        skill_dir = skill_md.parent
        valid, reason, frontmatter, text = validate_skill_dir(skill_dir)
        if not valid:
            errors.append({"path": str(skill_dir), "error": reason})
            continue
        parts = skill_dir.relative_to(repos_dir).parts
        if len(parts) < 2 or "__" not in parts[0]:
            errors.append({"path": str(skill_dir), "error": "unexpected output path layout"})
            continue
        owner, repo = parts[0].split("__", 1)
        records.append(
            SkillRecord(
                owner=owner,
                repo=repo,
                repo_url=f"https://github.com/{owner}/{repo}",
                local_path=str(skill_dir.relative_to(out_dir)),
                skill_dir_name=skill_dir.name,
                declared_name=str(frontmatter.get("name", "")).strip(),
                description=str(frontmatter.get("description", "")).strip(),
                license=str(frontmatter.get("license", "")).strip(),
                compatibility=str(frontmatter.get("compatibility", "")).strip(),
                metadata=frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {},
                discovered_from=[],
                registry_skill_ids=[],
                skill_md_sha256=sha256_text(text),
            )
        )

    write_index_files(out_dir, records, errors)
    print(f"Reindexed {len(records)} skills. Errors: {len(errors)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover and collect Agent Skills from registries, GitHub topics, and seed repositories."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Discover repos and copy valid skill folders into a local vault")
    collect.add_argument("--out", default="./skill-vault", help="Output directory for collected skills")
    collect.add_argument("--workers", type=int, default=6, help="Concurrent repository workers")
    collect.add_argument("--gh-token", default="", help="GitHub token. Falls back to GITHUB_TOKEN env var")
    collect.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    collect.add_argument("--retries", type=int, default=3, help="HTTP retry attempts")
    collect.add_argument("--min-delay", type=float, default=0.25, help="Minimum delay between HTTP requests")
    collect.add_argument(
        "--skills-api-base",
        default="",
        help="Base URL for a skills-api compatible registry, for example https://your-host/api",
    )
    collect.add_argument("--api-page-size", type=int, default=100, help="Page size for registry API discovery")
    collect.add_argument("--api-max-pages", type=int, default=0, help="Max registry pages. 0 means no explicit limit")
    collect.add_argument(
        "--skills-sh-html",
        action="store_true",
        default=True,
        help="Also scrape skills.sh homepage + trending pages for top-skill discovery",
    )
    collect.add_argument(
        "--topic",
        action="append",
        default=["agent-skills", "claude-code-skills", "claude-skills", "copilot-coding-agent"],
        help="GitHub topic to search. Repeat for more topics",
    )
    collect.add_argument("--topic-pages", type=int, default=2, help="GitHub topic pages to scan per topic")
    collect.add_argument("--seed-repo", action="append", default=[], help="Seed repo in owner/repo form. Repeatable")
    collect.add_argument("--seed-file", default="", help="Text file with one seed repo per line")
    collect.add_argument("--max-repos", type=int, default=0, help="Cap the number of repositories processed")
    collect.add_argument(
        "--registry-only",
        action="store_true",
        help="When registry data provides skill IDs, copy only those declared names from that repo",
    )
    collect.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Re-download repos even if they already exist in the output directory",
    )
    collect.add_argument("--debug", action="store_true", help="Print verbose discovery/fetch debug logs to stderr")
    collect.set_defaults(func=collect_command)

    reindex = sub.add_parser("reindex", help="Rebuild index.json / index.csv from an existing vault")
    reindex.add_argument("--out", default="./skill-vault", help="Existing output directory")
    reindex.set_defaults(func=reindex_command)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
