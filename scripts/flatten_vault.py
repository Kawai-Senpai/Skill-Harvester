#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Iterable, Tuple


def parse_owner_repo(repo_folder: str) -> Tuple[str, str]:
    if "__" in repo_folder:
        owner, repo = repo_folder.split("__", 1)
        return owner, repo
    return "unknown", "unknown"


def unique_dest(out_dir: Path, base_name: str) -> Path:
    candidate = out_dir / base_name
    if not candidate.exists():
        return candidate
    idx = 2
    while True:
        candidate = out_dir / f"{base_name}__{idx}"
        if not candidate.exists():
            return candidate
        idx += 1


def count_lines(path: Path) -> int:
    try:
        return len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return 0


def iter_files(root: Path) -> Iterable[Path]:
    for item in root.rglob("*"):
        if item.is_file():
            yield item


def has_only_metadata_files(skill_dir: Path) -> bool:
    allowed_names = {".collector-manifest.json"}
    allowed_exts = {".md", ".json", ".jsonl", ".csv", ".txt"}
    for file_path in iter_files(skill_dir):
        if file_path.name in allowed_names:
            continue
        if file_path.suffix.lower() in allowed_exts:
            continue
        return False
    return True


def should_skip_skill(skill_dir: Path, min_lines: int = 200) -> bool:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return False
    if count_lines(skill_md) >= min_lines:
        return False
    return has_only_metadata_files(skill_dir)


def flatten_vault(input_dir: Path, output_dir: Path, clean: bool) -> int:
    repos_dir = input_dir / "repos"
    if not repos_dir.exists():
        print(f"No repos directory found at {repos_dir}", file=sys.stderr)
        return 1

    if clean and output_dir.exists():
        shutil.rmtree(output_dir)

    output_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    collisions = 0
    skipped_small = 0

    for skill_md in repos_dir.rglob("SKILL.md"):
        skill_dir = skill_md.parent
        rel = skill_dir.relative_to(repos_dir)
        if len(rel.parts) < 2:
            continue
        repo_folder = rel.parts[0]
        owner, repo = parse_owner_repo(repo_folder)
        if should_skip_skill(skill_dir):
            skipped_small += 1
            continue
        skill_name = skill_dir.name
        dest = output_dir / skill_name
        if dest.exists():
            collisions += 1
            base_name = f"{skill_name}__{owner}__{repo}"
            dest = unique_dest(output_dir, base_name)
        shutil.copytree(skill_dir, dest)
        copied += 1

    print(f"Copied {copied} skills into {output_dir}")
    if skipped_small:
        print(f"Skipped {skipped_small} short metadata-only skills")
    if collisions:
        print(f"Resolved {collisions} name collisions by adding owner/repo suffixes")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flatten a harvested skill vault into a single folder of skills."
    )
    parser.add_argument("--in", dest="input_dir", default="./skill-vault", help="Input vault directory")
    parser.add_argument("--out", dest="output_dir", default="./skill-vault-flat", help="Output folder")
    parser.add_argument("--clean", action="store_true", help="Delete output folder before copying")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return flatten_vault(Path(args.input_dir).resolve(), Path(args.output_dir).resolve(), args.clean)


if __name__ == "__main__":
    raise SystemExit(main())
