import hashlib
import subprocess
import os
import logging
import shutil
import re
import argparse
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests


logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


class ForgeProvider(ABC):
    @abstractmethod
    def get_latest_release_tag(self, owner: str, repo: str) -> str | None:
        pass

    @abstractmethod
    def calculate_sha256(self, owner: str, repo: str, tag: str) -> str | None:
        pass

    @abstractmethod
    def calculate_commit(self, owner: str, repo: str, tag: str) -> str | None:
        pass

    @abstractmethod
    def archive_url(self, owner: str, repo: str, tag: str) -> str:
        pass

    @staticmethod
    def _sha256_from_url(url: str) -> str | None:
        sha256_hash = hashlib.sha256()
        try:
            response = requests.get(url, stream=True)
            response.raise_for_status()
            for chunk in response.iter_content(chunk_size=8192):
                sha256_hash.update(chunk)
            digest = sha256_hash.hexdigest()
            logger.info(f"SHA256 for {url}: {digest}")
            return digest
        except Exception as e:
            logger.error(f"Failed to download / hash {url}: {e}")
            return None


class GitHubProvider(ForgeProvider):
    BASE_API = "https://api.github.com"
    BASE_URL = "https://github.com"

    def get_latest_release_tag(self, owner: str, repo: str) -> str | None:
        url = f"{self.BASE_API}/repos/{owner}/{repo}/releases/latest"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            return resp.json()["tag_name"]
        except Exception as e:
            logger.error(f"[GitHub] Failed to get latest release tag: {e}")
            return None

    def archive_url(self, owner: str, repo: str, tag: str) -> str:
        return f"{self.BASE_URL}/{owner}/{repo}/archive/refs/tags/{tag}.tar.gz"

    def calculate_sha256(self, owner: str, repo: str, tag: str) -> str | None:
        return self._sha256_from_url(self.archive_url(owner, repo, tag))

    def calculate_commit(self, owner: str, repo: str, tag: str) -> str | None:
        url = f"{self.BASE_API}/repos/{owner}/{repo}/git/refs/tags/{tag}"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            sha = resp.json()["object"]["sha"]
            logger.info(f"[GitHub] Commit for {tag}: {sha}")
            return sha
        except Exception as e:
            logger.error(f"[GitHub] Failed to get commit for {tag}: {e}")
            return None


class GitLabProvider(ForgeProvider):
    def __init__(self, host: str = "https://gitlab.com"):
        self.host = host.rstrip("/")
        self.api = f"{self.host}/api/v4"

    def _project_id(self, owner: str, repo: str) -> str:
        return f"{owner}%2F{repo}"

    def get_latest_release_tag(self, owner: str, repo: str) -> str | None:
        pid = self._project_id(owner, repo)
        url = f"{self.api}/projects/{pid}/releases"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            releases = resp.json()
            if not releases:
                logger.error("[GitLab] No releases found.")
                return None
            return releases[0]["tag_name"]
        except Exception as e:
            logger.error(f"[GitLab] Failed to get latest release tag: {e}")
            return None

    def archive_url(self, owner: str, repo: str, tag: str) -> str:
        namespace = f"{owner}/{repo}" if owner else repo
        return f"{self.host}/{namespace}/-/archive/{tag}/{repo}-{tag}.tar.gz"

    def calculate_sha256(self, owner: str, repo: str, tag: str) -> str | None:
        return self._sha256_from_url(self.archive_url(owner, repo, tag))

    def calculate_commit(self, owner: str, repo: str, tag: str) -> str | None:
        pid = self._project_id(owner, repo)
        url = f"{self.api}/projects/{pid}/repository/tags/{tag}"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            sha = resp.json()["commit"]["id"]
            logger.info(f"[GitLab] Commit for {tag}: {sha}")
            return sha
        except Exception as e:
            logger.error(f"[GitLab] Failed to get commit for {tag}: {e}")
            return None


class CodebergProvider(ForgeProvider):
    BASE_API = "https://codeberg.org/api/v1"
    BASE_URL = "https://codeberg.org"

    def get_latest_release_tag(self, owner: str, repo: str) -> str | None:
        url = f"{self.BASE_API}/repos/{owner}/{repo}/releases?limit=1"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            releases = resp.json()
            if not releases:
                logger.error("[Codeberg] No releases found.")
                return None
            return releases[0]["tag_name"]
        except Exception as e:
            logger.error(f"[Codeberg] Failed to get latest release tag: {e}")
            return None

    def archive_url(self, owner: str, repo: str, tag: str) -> str:
        return f"{self.BASE_URL}/{owner}/{repo}/archive/{tag}.tar.gz"

    def calculate_sha256(self, owner: str, repo: str, tag: str) -> str | None:
        return self._sha256_from_url(self.archive_url(owner, repo, tag))

    def calculate_commit(self, owner: str, repo: str, tag: str) -> str | None:
        url = f"{self.BASE_API}/repos/{owner}/{repo}/tags/{tag}"
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            sha = resp.json()["id"]
            logger.info(f"[Codeberg] Commit for {tag}: {sha}")
            return sha
        except Exception as e:
            logger.error(f"[Codeberg] Failed to get commit for {tag}: {e}")
            return None


_PROVIDERS: dict[str, ForgeProvider] = {
    "github.com": GitHubProvider(),
    "codeberg.org": CodebergProvider(),
    "gitlab.com": GitLabProvider(),
}


def get_provider(host: str) -> ForgeProvider:
    if host in _PROVIDERS:
        return _PROVIDERS[host]
    logger.warning(f"Unknown host '{host}', assuming GitHub-compatible API.")
    return GitLabProvider(host=f"https://{host}")


def detect_provider_from_source(source_url: str) -> tuple[str, str, str, ForgeProvider] | None:
    url = source_url.split("::")[-1].strip().strip("(\"' )")

    parsed = urlparse(url)
    host = parsed.netloc
    parts = [p for p in parsed.path.split("/") if p]

    if len(parts) < 2:
        logger.error(f"Cannot parse owner/repo from source URL: {url}")
        return None

    if "/-/" in parsed.path:
        segments = parsed.path.split("/-/")[0].strip("/").split("/")
        owner = "/".join(segments[:-1])
        repo = segments[-1]
    elif "archive" in parts:
        archive_idx = parts.index("archive")
        repo_parts = parts[:archive_idx]
        owner = "/".join(repo_parts[:-1])
        repo = repo_parts[-1]
    else:
        owner, repo = parts[0], parts[1]

    return host, owner, repo, get_provider(host)


def setup_ssh_for_aur():
    git_email = os.environ.get("GIT_EMAIL", "pauron@archlinux.org")
    git_name = os.environ.get("GIT_NAME", "pauron")
    subprocess.run(["git", "config", "--global", "user.email", git_email], check=True)
    subprocess.run(["git", "config", "--global", "user.name", git_name], check=True)

    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True, mode=0o700)

    key_path = os.path.join(ssh_dir, "aur_key")
    aur_key = os.environ["AUR_SSH_KEY"]

    with open(key_path, "w") as f:
        f.write(aur_key)
        if not aur_key.endswith("\n"):
            f.write("\n")

    os.chmod(key_path, 0o600)

    try:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", key_path],
            capture_output=True, text=True, check=True,
        )
        logger.info(f"SSH key fingerprint: {result.stdout.strip()}")
    except subprocess.CalledProcessError as e:
        logger.error(f"SSH key validation failed: {e.stderr}")
        return

    try:
        subprocess.run(
            ["ssh-keyscan", "-H", "aur.archlinux.org"],
            stdout=open(os.path.join(ssh_dir, "known_hosts"), "w"),
            check=True,
        )
        logger.info("Added AUR host key to known_hosts")
    except Exception as e:
        logger.warning(f"Failed to add host key: {e}")

    ssh_config_path = os.path.join(ssh_dir, "config")
    existing_config = ""
    if os.path.exists(ssh_config_path):
        with open(ssh_config_path) as f:
            existing_config = f.read()

    if "Host aur.archlinux.org" not in existing_config:
        with open(ssh_config_path, "a") as f:
            f.write(f"\n# Added by Pauron\nHost aur.archlinux.org\n    IdentityFile {key_path}\n\n")

    try:
        result = subprocess.run(
            ["ssh", "-i", key_path, "-T", "aur@aur.archlinux.org"],
            capture_output=True, text=True, timeout=10,
        )
        logger.info(f"SSH test exit code: {result.returncode}")
        if result.stderr:
            logger.info(f"SSH stderr: {result.stderr}")
        if result.stdout:
            logger.info(f"SSH stdout: {result.stdout}")
    except Exception as e:
        logger.warning(f"SSH connection test failed: {e}")


def clone_and_parse(pkg_name: str, aur_repo: str) -> dict[str, str | None] | None:
    try:
        if os.path.exists(pkg_name):
            return parse_pkgbuild(os.path.join(pkg_name, "PKGBUILD"))

        logger.info(f"Cloning {aur_repo}")
        subprocess.run(["git", "clone", aur_repo], capture_output=True, text=True, check=True)

        pkgbuild_path = os.path.join(pkg_name, "PKGBUILD")
        if not os.path.exists(pkgbuild_path):
            raise FileNotFoundError("PKGBUILD not found")

        logger.info("Parsing PKGBUILD...")
        return parse_pkgbuild(pkgbuild_path)
    except subprocess.CalledProcessError as e:
        logger.error(f"Git clone failed: {e}\n  stdout: {e.stdout}\n  stderr: {e.stderr}")
        return None
    except Exception as e:
        logger.error(f"Error: {e}")
        return None


def parse_pkgbuild(path) -> dict[str, str | None]:
    metadata: dict[str, str | None] = {
        "pkgver": None,
        "sha256sums": None,
        "_commit": None,
        "source": None,
        "host": None,
        "owner_name": None,
        "repo_name": None,
    }

    with open(path) as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        for field in list(metadata.keys()):
            if line.startswith(f"{field}="):
                value = line.split("=", 1)[1].strip().split("#")[0].strip()
                metadata[field] = value

                if field == "source":
                    parsed = detect_provider_from_source(value)
                    if parsed:
                        host, owner, repo, _ = parsed
                        metadata["host"] = host
                        metadata["owner_name"] = owner
                        metadata["repo_name"] = repo
                break

    return metadata


def display_metadata(metadata):
    if not metadata:
        logger.error("No metadata to display")
        return
    logger.info("Extracted metadata:")
    for key, value in metadata.items():
        logger.info(f"  {key}: {value}")


def update_pkgbuild_file(file: str, new_pkgver: str, new_sha256: str, new_commit: str):
    with open(file) as f:
        content = f.read()

    content = re.sub(r"pkgver=.*", f"pkgver={new_pkgver}", content)
    content = re.sub(r"sha256sums=\('.*?'\)", f"sha256sums=('{new_sha256}')", content)
    content = re.sub(r"_commit=\('.*?'\)", f"_commit=('{new_commit}')", content)

    with open(file, "w") as f:
        f.write(content)
    logger.info("PKGBUILD updated successfully")


def update_dot_srcinfo_file(file: str, new_pkgver: str, new_sha256: str, new_archive_url: str):
    with open(file) as f:
        content = f.read()

    content = re.sub(r"pkgver = .*", f"pkgver = {new_pkgver}", content)
    content = re.sub(r"sha256sums = .*", f"sha256sums = {new_sha256}", content)

    source_match = re.search(r"^\s*source\s*=\s*(.+)", content, flags=re.MULTILINE)
    if source_match:
        old_source = source_match.group(1)
        new_source = re.sub(r"https://\S+\.tar\.gz", new_archive_url, old_source)
        content = content.replace(old_source, new_source)

    with open(file, "w") as f:
        f.write(content)
    logger.info(".SRCINFO file was updated successfully")


# def regenerate_srcinfo(repo_path: str):
#     try:
#         subprocess.run(
#             ["makepkg", "--printsrcinfo"],
#             cwd=repo_path,
#             stdout=open(f"{repo_path}/.SRCINFO", "w"),
#             stderr=subprocess.PIPE,
#             text=True,
#             check=True
#         )
#         print(".SRCINFO regenerated successfully")
#     except subprocess.CalledProcessError as e:
#         print("makepkg failed:")
#         print(e.stderr)
#         raise


def push_changes(latest_tag: str):
    subprocess.run(["git", "add", "."], check=True)
    commit_msg = latest_tag if latest_tag.startswith("v") else f"v{latest_tag}"
    subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    subprocess.run(["git", "push"], check=True)
    logger.info(f"Successfully committed and pushed {latest_tag}")


def get_git_config(key) -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--global", key],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def restore_git_config(key, value):
    if value is not None:
        subprocess.run(["git", "config", "--global", key, value], check=True)
    else:
        subprocess.run(["git", "config", "--global", "--unset", key], check=False)


def main():
    parser = argparse.ArgumentParser(description="Update AUR package from upstream forge releases")
    parser.add_argument("--pkg-name", "-p", required=True, help="AUR package name (e.g. k3sup)")
    parser.add_argument(
        "--provider", choices=["github", "gitlab", "codeberg", "auto"], default="github",
        help="Forge provider (default: github)",
    )
    parser.add_argument(
        "--gitlab-host", default="https://gitlab.com",
        help="Base URL for self-hosted GitLab/Forgejo instances (default: https://gitlab.com)",
    )
    args = parser.parse_args()

    pkg_name = args.pkg_name
    aur_repo = f"ssh://aur@aur.archlinux.org/{pkg_name}.git"
    original_email = get_git_config("user.email")
    original_name = get_git_config("user.name")

    try:
        setup_ssh_for_aur()

        logger.info(f"Processing AUR package: {aur_repo}")
        metadata = clone_and_parse(pkg_name, aur_repo)
        if not metadata:
            logger.error("Failed to parse PKGBUILD. Aborting.")
            return
        display_metadata(metadata)

        if args.provider == "auto":
            host = metadata.get("host")
            if not host:
                logger.error("Could not auto-detect forge host from source URL. Use --provider.")
                return
            provider = get_provider(host)
        elif args.provider == "github":
            provider = GitHubProvider()
        elif args.provider == "gitlab":
            provider = GitLabProvider(host=args.gitlab_host)
        elif args.provider == "codeberg":
            provider = CodebergProvider()

        owner = metadata["owner_name"]
        repo = metadata["repo_name"]

        logger.info(f"Checking latest release via {provider.__class__.__name__}…")
        latest_tag = provider.get_latest_release_tag(owner, repo)
        if not latest_tag:
            logger.error("Could not determine latest release tag. Aborting.")
            return

        current_version = metadata.get("pkgver")
        new_version = latest_tag.lstrip("v")

        if new_version == current_version:
            logger.info(
                f"Already up to date: {new_version} == {current_version}. Nothing to do."
            )
            return

        logger.info(f"Update available: {current_version} → {new_version}")

        new_sha = provider.calculate_sha256(owner, repo, latest_tag)
        new_commit = provider.calculate_commit(owner, repo, latest_tag)

        if not new_sha or not new_commit:
            logger.error("Failed to compute sha256 or commit hash. Aborting.")
            return

        for filename in ["PKGBUILD", ".SRCINFO"]:
            filepath = os.path.join(pkg_name, filename)
            if os.path.exists(filepath):
                old_path = f"{filepath}_old"
                os.rename(filepath, old_path)
                shutil.copy2(old_path, filepath)
            else:
                logger.warning(f"File {filepath} not found, skipping backup")

        update_pkgbuild_file(os.path.join(pkg_name, "PKGBUILD"), new_version, new_sha, new_commit)
        update_dot_srcinfo_file(
            os.path.join(pkg_name, ".SRCINFO"),
            new_version,
            new_sha,
            provider.archive_url(owner, repo, latest_tag),
        )

        for filename in ["PKGBUILD_old", ".SRCINFO_old"]:
            filepath = os.path.join(pkg_name, filename)
            if os.path.exists(filepath):
                os.remove(filepath)

        os.chdir(pkg_name)
        push_changes(latest_tag)

    finally:
        restore_git_config("user.email", original_email)
        restore_git_config("user.name", original_name)


if __name__ == "__main__":
    main()