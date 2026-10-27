"""scripts/check-secrets.sh against throwaway git repos: it must stop real-looking secrets, and must not cry wolf
(the story slug 'trump-musk-ukraine-...' once looked like an OpenAI key). Every value below is invented."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check-secrets.sh"
FAKE_NVIDIA = "nvapi" + "-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v2"
FAKE_TELEGRAM = "1234567" + "89:" + "AAFakeFakeFakeFakeFakeFakeFakeFakeFak"


@pytest.fixture()
def repo(tmp_path):
    def git(*a): return subprocess.run(["git", *a], cwd=tmp_path, capture_output=True, text=True, check=True)
    git("init", "-q"); git("config", "user.email", "t@example.com"); git("config", "user.name", "T")
    return tmp_path, git


def scan(repo_dir, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=repo_dir, capture_output=True, text=True)


def stage(repo, name, text):
    d, git = repo
    (d / name).parent.mkdir(parents=True, exist_ok=True); (d / name).write_text(text); git("add", name)


REAL_LOOKING = [
    "NVIDIA_API" + "_KEY=" + FAKE_NVIDIA + "\n",
    "token = '" + FAKE_TELEGRAM + "'\n",
    "FAL" + "_KEY=" + "abcd1234" + "efgh5678" + "ijkl9012" + "mnop\n",
    "x = '" + "sk" + "-" + "a" * 40 + "'\n",
    "key: " + "AK" + "IA" + "ABCDEFGHIJKLMNOP\n",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n",
    "TELEGRAM_BOT" + "_TOKEN=" + FAKE_TELEGRAM + "\n",
    "DB_PASS" + "WORD=" + "correcthorse" + "batterystaple99\n",
]


@pytest.mark.parametrize("text", REAL_LOOKING)
def test_stops_real_looking_secrets_and_hides_the_value(repo, text):
    stage(repo, "config.py", text)
    r = scan(repo[0])
    assert r.returncode == 1 and "REFUSED" in r.stderr
    assert FAKE_NVIDIA not in r.stderr and FAKE_TELEGRAM not in r.stderr and "correcthorse" not in r.stderr   # never echoes the secret


@pytest.mark.parametrize("text", [
    "NVIDIA_API_KEY=\n", "NVIDIA_API_KEY=nvapi-...\n", 'nvidia_api_key="nvapi-test"\n', "Bearer nvapi-test\n",
    "TELEGRAM_BOT_TOKEN=\n", "FAL_KEY=your-key-here\n", "key = os.environ['NVIDIA_API_KEY']\n",
    '"slug": "trump-musk-ukraine-starlink-access-restored-dispute",\n',               # contains "sk-" + letters: not a key
    '"src": "/media/trump-musk-ukraine-starlink-access-restored-dispute/cover.jpg"\n',
    "Set it in .env (starts with nvapi-)\n",
])
def test_does_not_cry_wolf_on_placeholders_empty_values_and_story_slugs(repo, text):
    stage(repo, "ok.txt", text)
    assert scan(repo[0]).returncode == 0, text


def test_a_real_env_file_is_refused_but_the_example_is_fine(repo):
    stage(repo, ".env", "SOMETHING=1\n")
    assert scan(repo[0]).returncode == 1
    d, git = repo; git("rm", "-q", "--cached", ".env")
    stage(repo, ".env.example", "SOMETHING=\n")
    assert scan(repo[0]).returncode == 0


def test_only_what_is_being_committed_is_judged(repo):
    d, git = repo
    (d / "later.py").write_text("KEY=" + FAKE_NVIDIA + "\n")           # not staged: not part of this commit
    stage(repo, "fine.py", "x = 1\n")
    assert scan(d).returncode == 0
    assert scan(d, "--all").returncode == 0                       # --all looks at tracked files only; later.py is untracked


def test_the_hook_actually_blocks_a_commit(repo):
    d, git = repo
    (d / ".githooks").mkdir(); (d / "scripts").mkdir()
    (d / "scripts/check-secrets.sh").write_text(SCRIPT.read_text()); (d / "scripts/check-secrets.sh").chmod(0o755)
    (d / ".githooks/pre-commit").write_text('#!/usr/bin/env bash\nexec "$(git rev-parse --show-toplevel)/scripts/check-secrets.sh"\n'); (d / ".githooks/pre-commit").chmod(0o755)
    git("config", "core.hooksPath", ".githooks")
    stage(repo, "leak.py", "KEY=" + FAKE_NVIDIA + "\n")
    blocked = subprocess.run(["git", "commit", "-m", "oops"], cwd=d, capture_output=True, text=True)
    assert blocked.returncode != 0 and "REFUSED" in blocked.stderr
    git("rm", "-q", "--cached", "leak.py")
    stage(repo, "fine.py", "x = 1\n")
    assert subprocess.run(["git", "commit", "-m", "ok"], cwd=d, capture_output=True, text=True).returncode == 0
