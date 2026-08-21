import base64
import hashlib
import json
import mimetypes
import re
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class PackagingApprovalRequired(RuntimeError):
    """Raised when package export is attempted without preview approval."""


@dataclass
class RedactionResult:
    text: str
    findings: list


class DynamicRedactor:
    """
    Builds a task-local redaction policy from context and content.

    The policy intentionally keeps only categories and placeholders. It does not
    preserve a raw value map, so sensitive originals cannot leak into exports.
    """

    SECRET_PATTERNS = [
        ("secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?([A-Za-z0-9_\-./+=]{8,})")),
        ("email", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
        ("phone", re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")),
        ("private_url", re.compile(r"https?://(?:localhost|127\.0\.0\.1|10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|[^/\s]+\.internal)[^\s]*")),
    ]

    MONEY_PATTERN = re.compile(r"(?<!\w)(?:[$¥€]\s*)?\d+(?:,\d{3})*(?:\.\d+)?\s*(?:USD|CNY|RMB|EUR|元|报价|价格|price)?(?!\w)", re.I)
    BUSINESS_NAME_PATTERN = re.compile(
        r"\b(?:vendor|supplier|customer|client|account|company)\s+[A-Z][A-Za-z0-9&._-]*(?:\s+[A-Z][A-Za-z0-9&._-]*)?\b",
        re.I,
    )
    PART_NUMBER_PATTERN = re.compile(r"\b(?:part|sku|pn|料号|物料号)[\s:#-]*[A-Za-z0-9][A-Za-z0-9._/-]{3,}\b", re.I)

    DOMAIN_HINTS = {
        "commercial": (
            "supplier",
            "vendor",
            "customer",
            "client",
            "quote",
            "price",
            "报价",
            "供应商",
            "客户",
            "价格",
            "料号",
        )
    }

    def __init__(self, task_context="", extra_rules=None):
        self.task_context = task_context or ""
        self.extra_rules = extra_rules or []
        self.policy = self._build_policy()

    def _build_policy(self):
        lowered = self.task_context.lower()
        enabled = {"hard": True, "commercial": False}
        if any(hint.lower() in lowered for hint in self.DOMAIN_HINTS["commercial"]):
            enabled["commercial"] = True
        return {
            "task_context_hash": hashlib.sha256(self.task_context.encode("utf-8")).hexdigest(),
            "enabled": enabled,
            "generated_at": datetime.now().isoformat(),
        }

    def redact_text(self, text):
        findings = []
        redacted = text

        for category, pattern in self.SECRET_PATTERNS:
            redacted, count = pattern.subn(lambda match: self._mark(category, findings, match.group(0)), redacted)
            if count:
                findings.append({"category": category, "count": count, "rule": "hard"})

        if self.policy["enabled"]["commercial"] or self._looks_commercial(redacted):
            redacted, count = self.BUSINESS_NAME_PATTERN.subn(
                lambda match: self._mark("business_name", findings, match.group(0)),
                redacted,
            )
            if count:
                findings.append({"category": "business_name", "count": count, "rule": "semantic"})

            redacted, count = self.PART_NUMBER_PATTERN.subn(
                lambda match: self._mark("part_number", findings, match.group(0)),
                redacted,
            )
            if count:
                findings.append({"category": "part_number", "count": count, "rule": "semantic"})

            redacted, count = self.MONEY_PATTERN.subn(
                lambda match: self._mark("commercial_number", findings, match.group(0)),
                redacted,
            )
            if count:
                findings.append({"category": "commercial_number", "count": count, "rule": "semantic"})

        for rule in self.extra_rules:
            category = rule.get("category", "custom")
            pattern = re.compile(rule["pattern"], rule.get("flags", 0))
            redacted, count = pattern.subn(
                lambda match: self._mark(category, findings, match.group(0)),
                redacted,
            )
            if count:
                findings.append({"category": category, "count": count, "rule": "custom"})

        return RedactionResult(text=redacted, findings=findings)

    def redact_trajectory(self, trajectory):
        redacted = json.loads(json.dumps(trajectory))
        findings = []
        for file_path, file_entry in redacted.get("files", {}).items():
            for version in file_entry.get("versions", []):
                if version.get("encoding") != "utf-8" or "content" not in version:
                    continue
                result = self.redact_text(version["content"])
                version["content"] = result.text
                version["redacted"] = bool(result.findings)
                if result.findings:
                    findings.append({"path": file_path, "commit": version.get("commit"), "findings": result.findings})
        redacted["redaction"] = {
            "policy": self.policy,
            "findings": findings,
            "placeholder_map_included": False,
        }
        return redacted

    def _looks_commercial(self, text):
        lowered = text.lower()
        return any(hint.lower() in lowered for hint in self.DOMAIN_HINTS["commercial"])

    def _mark(self, category, findings, value):
        del findings
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        return f"[REDACTED:{category}:{digest}]"


class GitVersionManager:
    """Creates git snapshots and extracts file trajectories across versions."""

    def __init__(self, repo_path=".", tracer=None, session_id=None):
        self.repo_path = Path(repo_path).resolve()
        self.tracer = tracer
        self.session_id = session_id

    def ensure_repo(self):
        if not (self.repo_path / ".git").exists():
            self._git("init")
        return self.repo_path

    def snapshot(self, message, paths=None, allow_empty=False):
        self.ensure_repo()
        add_paths = paths or ["."]
        self._git("add", *[str(path) for path in add_paths])
        commit_args = ["commit", "-m", message]
        if allow_empty:
            commit_args.insert(1, "--allow-empty")
        try:
            self._git(*commit_args)
        except subprocess.CalledProcessError as exc:
            if "nothing to commit" not in (exc.stderr or ""):
                raise
        commit = self._git("rev-parse", "HEAD")
        self._trace_git_event("git_snapshot", {"message": message, "commit": commit, "paths": add_paths})
        return commit

    def extract_trajectory(self, paths=None, rev_range=None, max_file_bytes=512 * 1024):
        self.ensure_repo()
        selected_paths = [str(path) for path in paths] if paths else self._tracked_files()
        files = {}
        for file_path in selected_paths:
            commits = self._commits_for_path(file_path, rev_range=rev_range)
            versions = []
            for commit in commits:
                content = self._show_file(commit["commit"], file_path, max_file_bytes=max_file_bytes)
                if content is None:
                    continue
                versions.append({**commit, **content})
            if versions:
                files[file_path] = {"versions": versions}

        trajectory = {
            "session_id": self.session_id,
            "generated_at": datetime.now().isoformat(),
            "git": {
                "repo_path": str(self.repo_path),
                "head": self._git("rev-parse", "HEAD", check=False) or None,
                "dirty": bool(self._git("status", "--porcelain", check=False)),
            },
            "files": files,
            "token_usage": self._token_usage(),
        }
        self._trace_git_event("git_trajectory_extract", {"paths": selected_paths, "file_count": len(files)})
        return trajectory

    def _tracked_files(self):
        output = self._git("ls-files", check=False)
        return [line for line in output.splitlines() if line]

    def _commits_for_path(self, file_path, rev_range=None):
        args = ["log", "--reverse", "--format=%H%x1f%ct%x1f%s"]
        if rev_range:
            args.append(rev_range)
        args.extend(["--", file_path])
        output = self._git(*args, check=False)
        commits = []
        for line in output.splitlines():
            parts = line.split("\x1f", 2)
            if len(parts) != 3:
                continue
            sha, timestamp, message = parts
            commits.append(
                {
                    "commit": sha,
                    "timestamp": datetime.fromtimestamp(int(timestamp)).isoformat(),
                    "message": message,
                }
            )
        return commits

    def _show_file(self, commit, file_path, max_file_bytes):
        blob = subprocess.run(
            ["git", "show", f"{commit}:{file_path}"],
            cwd=self.repo_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if blob.returncode != 0:
            return None
        data = blob.stdout
        content_hash = hashlib.sha256(data).hexdigest()
        entry = {
            "path": file_path,
            "sha256": content_hash,
            "size_bytes": len(data),
            "mime_type": mimetypes.guess_type(file_path)[0] or "application/octet-stream",
        }
        if len(data) > max_file_bytes:
            entry["content"] = f"[omitted: file larger than {max_file_bytes} bytes]"
            entry["encoding"] = "omitted"
            return entry
        try:
            entry["content"] = data.decode("utf-8")
            entry["encoding"] = "utf-8"
        except UnicodeDecodeError:
            entry["content_base64"] = base64.b64encode(data).decode("ascii")
            entry["encoding"] = "base64"
        return entry

    def _token_usage(self):
        if self.tracer is None or self.session_id is None:
            return None
        return self.tracer.get_token_usage(session_id=self.session_id)

    def _trace_git_event(self, name, data):
        if self.tracer is None:
            return
        self.tracer.add_trace(
            "EVENT",
            name,
            result=data,
            tags=["git", "trajectory"],
            session_id=self.session_id,
        )
        self.tracer.save_traces()

    def _git(self, *args, check=True):
        completed = subprocess.run(
            ["git", *args],
            cwd=self.repo_path,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return completed.stdout.strip()


class TrajectoryBuilder:
    """Builds redacted, approval-gated trajectory packages."""

    def __init__(self, repo_path=".", tracer=None, session_id=None, task_context="", redactor=None):
        self.repo_path = Path(repo_path).resolve()
        self.tracer = tracer
        self.session_id = session_id
        self.task_context = task_context
        self.redactor = redactor or DynamicRedactor(task_context=task_context)
        self.version_manager = GitVersionManager(self.repo_path, tracer=tracer, session_id=session_id)
        self._last_preview = None

    def preview_package(self, paths=None, rev_range=None):
        raw = self.version_manager.extract_trajectory(paths=paths, rev_range=rev_range)
        redacted = self.redactor.redact_trajectory(raw)
        preview_id = self._preview_id(redacted)
        redacted["approval"] = {
            "preview_id": preview_id,
            "confirmed": False,
            "requires_expert_confirmation": True,
        }
        self._last_preview = redacted
        self._trace_event("redaction_preview", {"preview_id": preview_id, "paths": list(redacted.get("files", {}).keys())})
        return redacted

    def write_preview_package(self, paths=None, output_dir=None, rev_range=None):
        preview = json.loads(json.dumps(self.preview_package(paths=paths, rev_range=rev_range)))
        preview_dir = self._coerce_directory_path(
            output_dir or f"agenttrace_preview_{preview['approval']['preview_id']}"
        )
        self._write_package_files(preview, preview_dir)
        trajectory_path = preview_dir / "trajectory-preview.json"
        trajectory_path.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8")
        self._trace_event(
            "redaction_preview_directory",
            {
                "preview_id": preview["approval"]["preview_id"],
                "preview_dir": str(preview_dir),
                "package_type": "preview-directory",
            },
        )
        self._last_preview = preview
        return {
            "preview_dir": str(preview_dir),
            "trajectory_path": str(trajectory_path),
            "package_type": "preview-directory",
            "preview_id": preview["approval"]["preview_id"],
        }

    def _coerce_directory_path(self, path):
        directory = Path(path)
        if directory.suffix:
            return directory.with_suffix("")
        return directory

    def export_package(self, paths=None, output_path=None, confirmed_preview_id=None, rev_range=None):
        if not confirmed_preview_id:
            raise PackagingApprovalRequired("Export requires a confirmed redaction preview id.")

        preview = self._last_preview or self.preview_package(paths=paths, rev_range=rev_range)
        if preview["approval"]["preview_id"] != confirmed_preview_id:
            preview = self.preview_package(paths=paths, rev_range=rev_range)
        if preview["approval"]["preview_id"] != confirmed_preview_id:
            raise PackagingApprovalRequired("Confirmed preview id does not match the current redacted package.")

        package = json.loads(json.dumps(preview))
        package["approval"] = {
            "confirmed": True,
            "confirmed_preview_id": confirmed_preview_id,
            "confirmed_at": datetime.now().isoformat(),
        }
        output = Path(output_path or f"agenttrace_trajectory_{confirmed_preview_id}.zip")
        if output.suffix.lower() != ".zip":
            output = output.with_suffix(".zip")
        package_dir = output.with_suffix("")

        self._write_package_files(package, package_dir)
        trajectory_path = package_dir / "trajectory.json"
        trajectory_path.write_text(json.dumps(package, ensure_ascii=False, indent=2), encoding="utf-8")
        self._zip_package(package_dir, output)

        self._trace_event(
            "trajectory_package_export",
            {
                "preview_id": confirmed_preview_id,
                "output_path": str(output),
                "package_dir": str(package_dir),
                "package_type": "zip",
            },
        )
        return {
            "output_path": str(output),
            "package_dir": str(package_dir),
            "package_type": "zip",
            "preview_id": confirmed_preview_id,
        }

    def _write_package_files(self, package, package_dir):
        latest_by_path = {}
        for file_path, file_entry in package.get("files", {}).items():
            for index, version in enumerate(file_entry.get("versions", []), start=1):
                package_file = self._write_version_file(package_dir, file_path, version, index)
                if package_file:
                    version["package_file"] = package_file
                    latest_by_path[file_path] = package_file
                self._strip_inline_content(version)

        for file_path, package_file in latest_by_path.items():
            source = package_dir / package_file
            latest = package_dir / "files" / "redacted" / "latest" / file_path
            latest.parent.mkdir(parents=True, exist_ok=True)
            latest.write_bytes(source.read_bytes())
            package["files"][file_path]["latest_package_file"] = self._relative_package_path(latest, package_dir)

        package["package"] = {
            "format": "agenttrace-redacted-trajectory",
            "version": 1,
            "layout": {
                "trajectory": "trajectory.json",
                "latest_files": "files/redacted/latest/",
                "versioned_files": "files/redacted/versions/",
            },
        }

    def _write_version_file(self, package_dir, file_path, version, index):
        commit = (version.get("commit") or f"version-{index}")[:12]
        target = package_dir / "files" / "redacted" / "versions" / commit / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if version.get("encoding") == "utf-8" and "content" in version:
            target.write_text(version["content"], encoding="utf-8")
        else:
            return None
        return self._relative_package_path(target, package_dir)

    def _strip_inline_content(self, version):
        version.pop("content", None)
        version.pop("content_base64", None)

    def _zip_package(self, package_dir, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as package_zip:
            for path in sorted(package_dir.rglob("*")):
                if path.is_file():
                    package_zip.write(path, self._relative_package_path(path, package_dir))

    def _relative_package_path(self, path, package_dir):
        return path.relative_to(package_dir).as_posix()

    def _preview_id(self, redacted_package):
        payload = json.dumps(redacted_package, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _trace_event(self, name, data):
        if self.tracer is None:
            return
        self.tracer.add_trace(
            "EVENT",
            name,
            result=data,
            tags=["redaction", "trajectory"],
            session_id=self.session_id,
        )
        self.tracer.save_traces()


def summarize_token_usage(rows):
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    by_model = {}
    for row in rows:
        prompt = int(row.get("prompt_tokens") or 0)
        completion = int(row.get("completion_tokens") or 0)
        total_tokens = int(row.get("total_tokens") or prompt + completion)
        total["prompt_tokens"] += prompt
        total["completion_tokens"] += completion
        total["total_tokens"] += total_tokens
        model = row.get("model") or "unknown"
        if model not in by_model:
            by_model[model] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        by_model[model]["prompt_tokens"] += prompt
        by_model[model]["completion_tokens"] += completion
        by_model[model]["total_tokens"] += total_tokens
    return {**total, "by_model": by_model, "entries": len(rows)}


def normalize_token_usage(token_usage):
    if token_usage is None:
        return None
    usage = dict(token_usage)
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + completion)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        **{key: value for key, value in usage.items() if key not in {"prompt_tokens", "completion_tokens", "total_tokens"}},
    }


def file_snapshot(path, max_bytes=1024 * 1024):
    file_path = Path(path).resolve()
    data = file_path.read_bytes()
    snapshot = {
        "path": str(file_path),
        "name": file_path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "mime_type": mimetypes.guess_type(str(file_path))[0] or "application/octet-stream",
        "captured_at": datetime.now().isoformat(),
    }
    if len(data) > max_bytes:
        snapshot["content"] = f"[omitted: file larger than {max_bytes} bytes]"
        snapshot["encoding"] = "omitted"
        return snapshot
    try:
        snapshot["content"] = data.decode("utf-8")
        snapshot["encoding"] = "utf-8"
    except UnicodeDecodeError:
        snapshot["content_base64"] = base64.b64encode(data).decode("ascii")
        snapshot["encoding"] = "base64"
    return snapshot


def new_artifact_id():
    return f"artifact_{datetime.now().isoformat()}_{uuid.uuid4().hex[:8]}"
