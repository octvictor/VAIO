"""Cut a new VAIO release: tag the current code and push the tag.

Pushing a tag whose name starts with "v" is what makes GitHub build
VAIO.exe and publish it - see .github/workflows/build-windows.yml. That is
the whole mechanism, and this script is the three git commands it takes,
with the checks that stop the two ways it goes quietly wrong: releasing
code that is not what is on GitHub, and reusing a tag that already points
somewhere else.

Normally run by double-clicking release.bat.
"""
from __future__ import annotations

import re
import subprocess
import sys
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if check and result.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def version_tuple(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag[1:].split("."))


def released_tags() -> list[str]:
    """Tags on the remote, which is the only place that counts - a tag that
    exists only on this machine has published nothing."""
    listing = git("ls-remote", "--tags", "origin")
    tags = set()
    for line in listing.splitlines():
        match = re.search(r"refs/tags/(v\d+(?:\.\d+)*)$", line)
        if match:
            tags.add(match.group(1))
    return sorted(tags, key=version_tuple)


def suggest_next(tags: list[str]) -> str:
    """Bump the minor: v1.1 -> v1.2. The three-part tags in this repo's
    history (v1.0.1) were same-day fixes to a release, so the next planned
    version after any of them is still a minor bump."""
    if not tags:
        return "v1.0"
    major, minor, *_ = (*version_tuple(tags[-1]), 0)
    return f"v{major}.{minor + 1}"


def ask(prompt: str, default: str = "") -> str:
    answer = input(prompt).strip()
    return answer or default


def main() -> int:
    print("VAIO - publish a new version\n")

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    print(f"Branch: {branch}")

    if git("status", "--porcelain"):
        print("\nYou have uncommitted changes. They will NOT be in this release -")
        print("a tag can only point at a commit, and these are not committed yet.")
        if ask("Carry on anyway? [y/N] ").lower() not in ("y", "yes"):
            return 0

    print("Fetching from GitHub...")
    git("fetch", "origin", branch, "--tags")

    # Releasing a commit GitHub does not have would build the wrong code:
    # the runner checks out the tag from the remote, not from this machine.
    local = git("rev-parse", "HEAD")
    remote = git("rev-parse", f"origin/{branch}", check=False)
    if remote and local != remote:
        ahead = git("rev-list", "--count", f"origin/{branch}..HEAD")
        behind = git("rev-list", "--count", f"HEAD..origin/{branch}")
        if int(behind or 0):
            print(f"\nGitHub has {behind} commit(s) you do not. Run 'git pull' first.")
            return 1
        if int(ahead or 0):
            print(f"\nYou have {ahead} commit(s) that are not on GitHub yet.")
            if ask("Push them now? [Y/n] ", "y").lower() in ("y", "yes"):
                git("push", "origin", branch)
                print("Pushed.")
            else:
                print("Stopping - the release would not contain them.")
                return 1

    tags = released_tags()
    print(f"\nReleased so far: {', '.join(tags) if tags else 'nothing yet'}")
    default = suggest_next(tags)
    version = ask(f"New version [{default}]: ", default)
    if not version.startswith("v"):
        version = "v" + version
    if not re.fullmatch(r"v\d+(\.\d+)*", version):
        print(f"'{version}' is not a version number - expected something like v1.2")
        return 1
    if version in tags:
        print(f"\n{version} is already published. Pick a number above {tags[-1]}.")
        return 1

    # The trap this exists to close: a leftover local tag from an abandoned
    # attempt still points at whatever was checked out that day, and
    # pushing it publishes that old code under a new version number. Git
    # says "already exists" and stops, which is easy to read as "fine, it
    # is already done" and skip past.
    existing = git("rev-parse", "-q", "--verify", f"refs/tags/{version}", check=False)
    if existing:
        if existing == local:
            print(f"\n{version} already exists here, on the right commit - pushing it.")
        else:
            subject = git("log", "-1", "--format=%s", existing)
            print(f"\n{version} already exists here but points at older code:")
            print(f"  {existing[:7]} {subject}")
            if ask("Move it to the current commit? [Y/n] ", "y").lower() not in ("y", "yes"):
                print("Stopping - pushing it as-is would publish that older code.")
                return 1
            git("tag", "-d", version)
            git("tag", version)
    else:
        git("tag", version)

    print(f"\n{version} -> {local[:7]} {git('log', '-1', '--format=%s', local)}")
    if ask(f"Publish {version}? [Y/n] ", "y").lower() not in ("y", "yes"):
        git("tag", "-d", version)
        print("Stopping - nothing published.")
        return 0

    print("Pushing the tag...")
    result = subprocess.run(
        ["git", "push", "origin", version], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        # Leave no local tag behind on failure: that is precisely what turns
        # into the stale tag handled above.
        git("tag", "-d", version, check=False)
        print("\nThe push failed:")
        print(result.stderr.strip())
        return 1

    print(f"\n{version} is published. GitHub is building VAIO.exe now - about 2 minutes.")
    url = git("remote", "get-url", "origin")
    url = re.sub(r"(\.git)?$", "", url).replace("git@github.com:", "https://github.com/")
    print(f"\n  Progress:  {url}/actions")
    print(f"  Download:  {url}/releases")
    print("\nWhen the build goes green the .exe is on the releases page.")
    print("Put it over your old VAIO.exe and keep the data folder beside it.")
    try:
        webbrowser.open(f"{url}/actions")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
