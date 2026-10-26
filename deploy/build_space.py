"""Package the public demo as a Hugging Face Space (Docker SDK) in build/space/.

    python deploy/build_space.py --space <your-hf-username>/agentguard

The Space runs only the dashboard in demo mode (AGENTGUARD_DEMO=1): in-memory Redis,
the mock cloud, the eval suite. No gateway port, no API keys, no real cloud: there is
nothing in it worth attacking. Files come from git (tracked + new, minus .gitignore),
so .env and other local secrets can never be copied.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "build" / "space"

SPACE_README = """---
title: AgentGuard
emoji: 🛡️
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: A safety gateway for AI agents that run cloud infra
---

# AgentGuard: live demo

A safety gateway between AI agents and their tools: it stops runaway loops, runaway
spend (tools *and* LLM tokens), unapproved destructive actions and duplicate actions
after dropped connections.

This Space is the **public demo**: the dashboard, a scripted misbehaving on-call agent
going through the real gateway, and the full eval suite (30 bad agents, 12 good ones),
all in-process on in-memory data with a mock cloud. No API keys, no real infrastructure.

Source, design decisions and results: {github}
"""

SPACE_DOCKERFILE = """# Public demo only: dashboard in demo mode. No secrets, no gateway port.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    AGENTGUARD_DEMO=1 HOME=/home/user PATH=/home/user/.local/bin:$PATH
# Hugging Face runs Spaces as uid 1000.
RUN useradd --create-home --uid 1000 user
WORKDIR /home/user/app
COPY --chown=user . .
USER user
RUN pip install --user ".[ui,demo]"
EXPOSE 7860
CMD ["streamlit", "run", "ui/app.py", "--server.address", "0.0.0.0", "--server.port", "7860", \\
     "--server.headless", "true", "--browser.gatherUsageStats", "false"]
"""


def git_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if f and (ROOT / f).is_file()]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--space", help="your-hf-username/space-name (only used in the printed instructions)")
    p.add_argument("--github", default="https://github.com/dharsinikannan/AgentGuard")
    args = p.parse_args()

    if OUT.exists():
        shutil.rmtree(OUT)
    files = git_files()
    for rel in files:
        if rel.startswith(("build/", "deploy/")) or rel in ("Dockerfile", "docker-compose.yml", ".env"):
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dst)
    (OUT / "README.md").write_text(SPACE_README.format(github=args.github), encoding="utf-8")
    (OUT / "Dockerfile").write_text(SPACE_DOCKERFILE, encoding="utf-8")
    assert not (OUT / ".env").exists()
    print(f"packaged {len(list(OUT.rglob('*')))} files into {OUT}")
    space = args.space or "<your-hf-username>/agentguard"
    print(f"""
Next (once): create the Space at https://huggingface.co/new-space  (SDK: Docker, template: Blank)
Then push:
  cd {OUT}
  git init -b main
  git add -A
  git commit -m "AgentGuard public demo"
  git remote add space https://huggingface.co/spaces/{space}
  git push --force space main        # username + a Hugging Face *write* access token as the password
""")


if __name__ == "__main__":
    main()
