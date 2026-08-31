"""Publish the generated Swing report to a GitHub repository via API."""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import error, parse, request

from .config import (
    execution_feedback_history_path,
    execution_feedback_path,
    live_performance_path,
    report_path,
)


GITHUB_API = "https://api.github.com"


def _github_headers(token: str) -> Dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "swing-trading-qinglong",
    }


def _content_url(repo: str, target: str, branch: Optional[str] = None) -> str:
    url = f"{GITHUB_API}/repos/{repo}/contents/{target.lstrip('/')}"
    if branch:
        url = f"{url}?{parse.urlencode({'ref': branch})}"
    return url


def _github_json_request(
    method: str,
    url: str,
    token: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = _github_headers(token)
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        if exc.code == 404:
            return 404, {}
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API request failed: HTTP {exc.code} {details}") from exc


def _existing_file_sha(repo: str, target: str, branch: str, token: str) -> Optional[str]:
    status, payload = _github_json_request("GET", _content_url(repo, target, branch), token)
    if status == 404:
        return None
    sha = payload.get("sha")
    if not sha:
        raise RuntimeError(f"GitHub content payload does not include a sha for {target}")
    return str(sha)


def publish_report(
    source: Optional[Path] = None,
    target: Optional[str] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    source = Path(source or os.environ.get("REPORT_SOURCE", report_path()))
    target = target or os.environ.get("PUBLISH_TARGET", "index.html")
    repo = repo or os.environ.get("GITHUB_REPO")
    branch = branch or os.environ.get("GIT_BRANCH", "main")
    token = token or os.environ.get("GITHUB_TOKEN")

    if not source.is_file():
        raise FileNotFoundError(f"Report does not exist: {source}")
    if not repo:
        raise RuntimeError("GITHUB_REPO is required, for example: ok1991/Swing-trading")
    if "/" not in repo:
        raise RuntimeError("GITHUB_REPO must use owner/repo format, for example: ok1991/Swing-trading")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required to update index.html through the GitHub API")

    content = base64.b64encode(source.read_bytes()).decode("ascii")
    sha = _existing_file_sha(repo, target, branch, token)
    message = f"Update Swing V4 report {datetime.now():%Y-%m-%d %H:%M:%S}"
    payload: Dict[str, Any] = {
        "message": message,
        "content": content,
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    _, result = _github_json_request("PUT", _content_url(repo, target), token, payload)
    print(f"Report published to {repo}/{target} on {branch}")
    return result


def publish_execution_outputs(
    *,
    report_source: Optional[Path] = None,
    feedback_source: Optional[Path] = None,
    feedback_history_source: Optional[Path] = None,
    performance_source: Optional[Path] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Publish structured feedback first, then the human report."""
    feedback_result = publish_feedback(
        source=Path(feedback_source or execution_feedback_path()),
        history_source=Path(
            feedback_history_source or execution_feedback_history_path()
        ),
        repo=repo,
        branch=branch,
        token=token,
    )
    performance_result = publish_live_performance(
        source=Path(performance_source or live_performance_path()),
        repo=repo,
        branch=branch,
        token=token,
    )
    report_result = publish_report(
        source=Path(report_source or report_path()),
        target=os.environ.get("PUBLISH_TARGET", "index.html"),
        repo=repo,
        branch=branch,
        token=token,
    )
    return {
        "feedback": feedback_result,
        "performance": performance_result,
        "report": report_result,
    }


def publish_feedback(
    *,
    source: Optional[Path] = None,
    history_source: Optional[Path] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    history_result = publish_report(
        source=Path(history_source or execution_feedback_history_path()),
        target=os.environ.get(
            "FEEDBACK_HISTORY_PUBLISH_TARGET", "execution_feedback_history.json"
        ),
        repo=repo,
        branch=branch,
        token=token,
    )
    latest_result = publish_report(
        source=Path(source or execution_feedback_path()),
        target=os.environ.get("FEEDBACK_PUBLISH_TARGET", "execution_feedback_latest.json"),
        repo=repo,
        branch=branch,
        token=token,
    )
    return {"history": history_result, "latest": latest_result}


def publish_live_performance(
    *,
    source: Optional[Path] = None,
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    source_path = Path(source or live_performance_path())
    if not source_path.is_file():
        return {"status": "SKIPPED_NO_LIVE_PERFORMANCE"}
    return publish_report(
        source=source_path,
        target=os.environ.get(
            "LIVE_PERFORMANCE_PUBLISH_TARGET", "live_performance_latest.json"
        ),
        repo=repo,
        branch=branch,
        token=token,
    )


def main() -> None:
    publish_report()


if __name__ == "__main__":
    main()
