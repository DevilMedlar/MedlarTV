"""Build the separate public catalog; publish only with explicit --publish.

Execute this file directly to avoid MedlarTV's application package initializer.
The existing catalog URL identifies the intended GitHub repository. A checkout
path must be supplied; missing/wrong destinations are never created or adopted.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_REPOSITORY = "devilmedlar/medlar-commands-site"
GENERATED_PATHS = ("docs/index.html", "docs/commands.json")


class WorkflowError(Exception):
    pass


def is_expected_remote(value):
    """Exact GitHub identity; reject embedded credentials and other hosts."""
    value = value.strip()
    if value.startswith("git@github.com:"):
        repository = value[len("git@github.com:"):]
    else:
        parsed = urlsplit(value)
        if parsed.scheme == "https":
            if parsed.username is not None or parsed.password is not None:
                return False
        elif parsed.scheme == "ssh":
            if parsed.username != "git" or parsed.password is not None:
                return False
        else:
            return False
        if parsed.hostname != "github.com" or parsed.port is not None or parsed.query or parsed.fragment:
            return False
        repository = parsed.path.lstrip("/")
    if repository.endswith(".git"):
        repository = repository[:-4]
    return repository.casefold() == EXPECTED_REPOSITORY


class CatalogUpdate:
    def __init__(self, public_repo, publish=False):
        self.public = Path(public_repo).resolve()
        self.publish = publish
        # Caller Git environment must not redirect -C operations or the index.
        self.env = {key: value for key, value in os.environ.items()
                    if not key.upper().startswith("GIT_")}
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        self.base = self.branch = None

    def run(self, arguments, operation, accepted=(0,)):
        try:
            result = subprocess.run(arguments, cwd=ROOT, env=self.env,
                shell=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
        except OSError:
            raise WorkflowError(f"{operation} could not start; no subsequent action ran") from None
        if result.returncode not in accepted:
            raise WorkflowError(f"{operation} failed (exit {result.returncode}); no subsequent action ran")
        return result

    def git(self, *arguments, accepted=(0,)):
        return self.run(["git", "-C", str(self.public), *arguments],
                        "Git " + arguments[0], accepted)

    def require_clean_index(self):
        result = self.git("diff", "--cached", "--quiet", accepted=(0, 1))
        if result.returncode:
            raise WorkflowError("Public checkout has staged work; leave it untouched and review it first")

    def validate_origin(self):
        for options in (("remote", "get-url", "--all", "origin"),
                        ("remote", "get-url", "--push", "--all", "origin")):
            urls = self.git(*options).stdout.splitlines()
            try:
                matches = len(urls) == 1 and is_expected_remote(urls[0])
            except ValueError:
                matches = False
            if not matches:
                raise WorkflowError("Origin identity does not match DevilMedlar/medlar-commands-site")

    def validate_destination(self):
        if not self.public.is_dir():
            raise WorkflowError("Public checkout is missing; supply an existing --public-repo path")
        if self.public.is_relative_to(ROOT) or ROOT.is_relative_to(self.public):
            raise WorkflowError("Public checkout must be separate from the private project")
        top = self.git("rev-parse", "--show-toplevel").stdout.strip()
        if not top or Path(top).resolve() != self.public:
            raise WorkflowError("Destination must be the exact public Git checkout root")
        self.validate_origin()
        self.require_clean_index()
        for relative in ("docs", *GENERATED_PATHS):
            target = self.public / relative
            if target.resolve() != target or target.is_symlink():
                raise WorkflowError("Generated output must stay inside the public checkout")
            if target.exists() and (not target.is_dir() if relative == "docs" else not target.is_file()):
                raise WorkflowError("Generated output path has the wrong file type")
        if self.git("diff", "--quiet", "--", *GENERATED_PATHS, accepted=(0, 1)).returncode:
            raise WorkflowError("Generated files already have local edits; review them before rebuilding")
        tracked = set(self.git("ls-files", "--cached", "--", *GENERATED_PATHS).stdout.splitlines())
        if any((self.public / relative).exists() and relative not in tracked for relative in GENERATED_PATHS):
            raise WorkflowError("Generated files already exist untracked or ignored; review them before rebuilding")
        if self.publish:
            self.branch = self.git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", self.branch) or ".." in self.branch:
                raise WorkflowError("Publish requires a normal named branch")
            self.git("check-ref-format", "--branch", self.branch)
            upstream = self.git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}").stdout.strip()
            if upstream != "origin/" + self.branch:
                raise WorkflowError("Publish requires this branch to track the same origin branch")
            self.base = self.git("rev-parse", "--verify", "HEAD").stdout.strip()
            tracking = self.git("rev-parse", "--verify", "refs/remotes/origin/" + self.branch).stdout.strip()
            if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", self.base) or self.base != tracking:
                raise WorkflowError("Publish refuses pre-existing unpushed or divergent commits")

    def execute(self):
        self.validate_destination()
        self.run([sys.executable, "-B", str(ROOT / "MedlarTV/tools/build_commands_site.py"),
                  "--out", str(self.public / "docs")], "Catalog build")
        for relative in GENERATED_PATHS:
            target = self.public / relative
            if not target.is_file() or target.resolve() != target or target.is_symlink():
                raise WorkflowError("Build did not produce safe catalog files; no staging ran")
        if not self.publish:
            print("[OK] Catalog built locally. No staging, commit or push requested.")
            return
        # Recheck user work immediately before mutating the intended index.
        self.require_clean_index()
        self.validate_origin()
        if self.git("rev-parse", "--verify", "HEAD").stdout.strip() != self.base:
            raise WorkflowError("Public HEAD changed during build; no staging ran")
        self.git("add", "--", *GENERATED_PATHS)
        pending = self.git("diff", "--cached", "--name-only", "-z").stdout
        names = {name for name in pending.split("\0") if name}
        if not names:
            print("[OK] Catalog unchanged. No commit or push ran.")
            return
        if not names.issubset(GENERATED_PATHS):
            raise WorkflowError("Index contains unrelated staged work; no commit or push ran")
        # Path-only commit protects unrelated index entries even if they arrive
        # after the check. Empty hook directory avoids hook-driven extra changes.
        with tempfile.TemporaryDirectory(prefix="medlar-catalog-hooks-") as hooks:
            self.git("-c", "core.hooksPath=" + hooks, "commit", "--only", "-m", "Update commands",
                     "--", *GENERATED_PATHS)
            head = self.git("rev-parse", "--verify", "HEAD").stdout.strip()
            parent = self.git("rev-parse", "--verify", "HEAD^").stdout.strip()
            changed = self.git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "HEAD").stdout
            files = {name for name in changed.split("\0") if name}
            if parent != self.base or not files or not files.issubset(GENERATED_PATHS):
                raise WorkflowError("New commit does not contain only this catalog update; no push ran")
            if self.git("rev-parse", "--verify", "HEAD").stdout.strip() != head:
                raise WorkflowError("Public HEAD changed after commit; no push ran")
            self.validate_origin()
            self.git("-c", "core.hooksPath=" + hooks, "-c", "push.followTags=false",
                     "push", "origin", head + ":refs/heads/" + self.branch)
        print("[OK] Catalog commit pushed to the verified origin branch.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the separate public command catalog; publish is opt-in")
    parser.add_argument("--public-repo", required=True, help="Existing DevilMedlar/medlar-commands-site checkout root")
    parser.add_argument("--publish", action="store_true", help="Stage only generated files, commit and push their new commit")
    args = parser.parse_args(argv)
    try:
        CatalogUpdate(args.public_repo, publish=args.publish).execute()
    except WorkflowError as exc:
        print("[ERROR] " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
