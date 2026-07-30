from __future__ import annotations

import hashlib
import imaplib
import json
import logging
import re
import ssl
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from email import policy
from email.parser import BytesHeaderParser, BytesParser
from pathlib import Path
from typing import Any, Protocol

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")
UID_PATTERN = re.compile(rb"\bUID\s+(\d+)\b", re.I)
LIST_PATTERN = re.compile(
    rb"^\((?P<flags>[^)]*)\)\s+(?:NIL|\"(?:\\.|[^\"])*\")\s+(?P<name>.+)$"
)
logger = logging.getLogger(__name__)


class HistoryIMAPSettings(BaseSettings):
    """Credentials dedicated to the one-time historical RAG export."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="RAG_IMAP_",
        extra="ignore",
        case_sensitive=False,
    )

    address: str | None = None
    app_password: str | None = None
    host: str = "imap.gmail.com"
    port: int = 993
    folder: str = "[Gmail]/All Mail"
    max_download_mb: int = 2048
    connect_timeout_seconds: int = 15

    @field_validator("address", mode="before")
    @classmethod
    def normalize_address(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().casefold() or None
        return value

    @field_validator("app_password", mode="before")
    @classmethod
    def normalize_app_password(cls, value: object) -> object:
        if isinstance(value, str):
            return value.replace(" ", "") or None
        return value

    @field_validator("port", "max_download_mb", "connect_timeout_seconds")
    @classmethod
    def positive_number(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("IMAP port and download limit must be positive")
        return value


class IMAPClient(Protocol):
    def login(self, user: str, password: str) -> Any: ...

    def select(self, mailbox: str, readonly: bool = False) -> Any: ...

    def list(self, directory: str = "", pattern: str = "*") -> Any: ...

    def uid(self, command: str, *args: Any) -> Any: ...

    def logout(self) -> Any: ...


IMAPFactory = Callable[..., IMAPClient]


@dataclass(frozen=True)
class LocalExport:
    source_path: Path
    relative_path: Path
    message_id: str | None
    has_body_payload: bool


@dataclass(frozen=True)
class SupplementReport:
    raw_eml_files: int
    already_complete_local: int
    missing_body_local: int
    missing_body_with_message_id: int
    missing_body_without_message_id: int
    remote_uids_scanned: int
    remote_message_ids_scanned: int
    used_cached_remote_index: bool
    matched_message_ids: int
    unmatched_message_ids: tuple[str, ...]
    pending_before_run: int
    pending_after_run: int
    downloaded_messages: int
    downloaded_bytes: int
    copied_complete_files: int
    output_complete_files: int
    stopped_at_download_limit: bool
    stopped_at_message_limit: bool
    read_only: bool = True
    fetch_mode: str = "BODY.PEEK[]"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_message_id(value: str | None) -> str | None:
    if not value:
        return None
    match = MESSAGE_ID_PATTERN.search(value)
    if match:
        return match.group(0).casefold()
    normalized = value.strip().casefold()
    return normalized or None


def has_body_payload(raw: bytes) -> bool:
    """Return whether an export contains usable inline text for RAG parsing."""

    body_found = False
    for separator in (b"\r\n\r\n", b"\n\n", b"\r\r"):
        position = raw.find(separator)
        if position >= 0:
            body_found = bool(raw[position + len(separator) :].strip())
            break
    if not body_found:
        return False

    message = BytesParser(policy=policy.default).parsebytes(raw)
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (KeyError, LookupError, TypeError, UnicodeError, ValueError):
            content = part.get_payload(decode=True)
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, bytes) and content.strip():
            return True
    return False


def inspect_local_exports(raw_dir: Path) -> list[LocalExport]:
    parser = BytesHeaderParser(policy=policy.default)
    result: list[LocalExport] = []
    for path in sorted(raw_dir.rglob("*")):
        if not path.is_file() or path.suffix.casefold() != ".eml":
            continue
        raw = path.read_bytes()
        message = parser.parsebytes(raw, headersonly=True)
        result.append(
            LocalExport(
                source_path=path,
                relative_path=path.relative_to(raw_dir),
                message_id=normalize_message_id(
                    str(message.get("Message-ID")) if message.get("Message-ID") else None
                ),
                has_body_payload=has_body_payload(raw),
            )
        )
    return result


def _mailbox_argument(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _chunks(values: list[bytes], size: int) -> Iterable[list[bytes]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _iter_fetch_payloads(response: Any) -> Iterable[tuple[bytes, bytes]]:
    if not isinstance(response, list):
        return
    for item in response:
        if (
            isinstance(item, tuple)
            and len(item) >= 2
            and isinstance(item[0], bytes)
            and isinstance(item[1], bytes)
        ):
            yield item[0], item[1]


def _extract_uid(metadata: bytes) -> int | None:
    match = UID_PATTERN.search(metadata)
    return int(match.group(1)) if match else None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.part")
    temporary_path.write_bytes(payload)
    temporary_path.replace(path)


def _output_target(output_dir: Path, relative_path: Path) -> Path:
    bucket = relative_path.parts[0] if len(relative_path.parts) > 1 else "root"
    digest = hashlib.sha256(
        relative_path.as_posix().encode("utf-8", errors="replace")
    ).hexdigest()[:24]
    return output_dir / bucket / f"{digest}.eml"


def _write_source_map(
    exports: list[LocalExport],
    *,
    output_dir: Path,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_map_path = output_dir / "_source_map.jsonl"
    with source_map_path.open("w", encoding="utf-8", newline="\n") as stream:
        for item in exports:
            stream.write(
                json.dumps(
                    {
                        "source_file": item.relative_path.as_posix(),
                        "complete_file": _output_target(
                            output_dir,
                            item.relative_path,
                        ).relative_to(output_dir).as_posix(),
                        "message_id": item.message_id,
                        "source_had_usable_body": item.has_body_payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            stream.write("\n")


def _copy_existing_complete(
    exports: list[LocalExport],
    *,
    output_dir: Path,
) -> int:
    copied = 0
    for item in exports:
        if not item.has_body_payload:
            continue
        target = _output_target(output_dir, item.relative_path)
        if target.exists() and has_body_payload(target.read_bytes()):
            continue
        _atomic_write(target, item.source_path.read_bytes())
        copied += 1
    return copied


class ReadOnlyHistoryDownloader:
    def __init__(
        self,
        settings: HistoryIMAPSettings,
        *,
        client_factory: IMAPFactory = imaplib.IMAP4_SSL,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self._active_folder = settings.folder

    @staticmethod
    def _discover_all_mail_folder(client: IMAPClient) -> str | None:
        status, response = client.list()
        if status != "OK" or not isinstance(response, list):
            return None
        for item in response:
            if not isinstance(item, bytes):
                continue
            match = LIST_PATTERN.match(item.strip())
            if not match or b"\\all" not in match.group("flags").lower():
                continue
            raw_name = match.group("name").strip()
            if raw_name.startswith(b'"') and raw_name.endswith(b'"'):
                raw_name = raw_name[1:-1]
                raw_name = raw_name.replace(b'\\"', b'"').replace(b"\\\\", b"\\")
            try:
                return raw_name.decode("ascii")
            except UnicodeDecodeError:
                return raw_name.decode("utf-8", errors="replace")
        return None

    def _connect(self) -> IMAPClient:
        if not self.settings.address or not self.settings.app_password:
            raise ValueError(
                "RAG_IMAP_ADDRESS and RAG_IMAP_APP_PASSWORD must be set in .env"
            )
        client = self.client_factory(
            self.settings.host,
            self.settings.port,
            timeout=self.settings.connect_timeout_seconds,
        )
        try:
            client.login(self.settings.address, self.settings.app_password)
            status, _ = client.select(
                _mailbox_argument(self.settings.folder),
                readonly=True,
            )
            if status != "OK":
                discovered_folder = self._discover_all_mail_folder(client)
                if discovered_folder is None:
                    raise RuntimeError(
                        f"unable to select IMAP folder: {self.settings.folder}; "
                        "no folder marked \\All was found"
                    )
                status, _ = client.select(
                    _mailbox_argument(discovered_folder),
                    readonly=True,
                )
                if status != "OK":
                    raise RuntimeError(
                        f"unable to select auto-discovered IMAP folder: {discovered_folder}"
                    )
                self._active_folder = discovered_folder
                logger.info("Auto-discovered Gmail All Mail folder: %s", discovered_folder)
            else:
                self._active_folder = self.settings.folder
            return client
        except Exception:
            self._logout_quietly(client)
            raise

    @staticmethod
    def _logout_quietly(client: IMAPClient | None) -> None:
        if client is None:
            return
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError, ssl.SSLError):
            pass

    @staticmethod
    def _search_all_uids(client: IMAPClient) -> list[bytes]:
        status, data = client.uid("search", None, "ALL")
        if status != "OK":
            raise RuntimeError("IMAP UID search failed")
        if not data or not isinstance(data[0], bytes):
            return []
        return data[0].split()

    @staticmethod
    def _map_message_ids(
        client: IMAPClient,
        remote_uids: list[bytes],
        target_message_ids: set[str],
    ) -> tuple[dict[str, int], int]:
        parser = BytesHeaderParser(policy=policy.default)
        matches: dict[str, int] = {}
        scanned_message_ids = 0
        for batch in _chunks(remote_uids, 500):
            uid_set = b",".join(batch).decode("ascii")
            status, response = client.uid(
                "fetch",
                uid_set,
                "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])",
            )
            if status != "OK":
                raise RuntimeError("IMAP Message-ID header scan failed")
            for metadata, header_payload in _iter_fetch_payloads(response):
                uid = _extract_uid(metadata)
                message = parser.parsebytes(header_payload, headersonly=True)
                message_id = normalize_message_id(
                    str(message.get("Message-ID"))
                    if message.get("Message-ID")
                    else None
                )
                if uid is None or message_id is None:
                    continue
                scanned_message_ids += 1
                if message_id in target_message_ids:
                    matches.setdefault(message_id, uid)
            if len(matches) == len(target_message_ids):
                break
            if scanned_message_ids and scanned_message_ids % 2000 < 500:
                logger.info(
                    "Scanned %s remote Message-ID headers; matched %s of %s targets",
                    scanned_message_ids,
                    len(matches),
                    len(target_message_ids),
                )
        return matches, scanned_message_ids

    @staticmethod
    def _fetch_full_message(client: IMAPClient, uid: int) -> bytes:
        status, response = client.uid(
            "fetch",
            str(uid),
            "(UID BODY.PEEK[])",
        )
        if status != "OK":
            raise RuntimeError(f"IMAP full-message fetch failed for UID {uid}")
        candidates = [
            payload
            for _, payload in _iter_fetch_payloads(response)
            if payload
        ]
        if not candidates:
            raise RuntimeError(f"IMAP returned no message body for UID {uid}")
        raw = max(candidates, key=len)
        if not has_body_payload(raw):
            raise RuntimeError(f"IMAP returned a header-only message for UID {uid}")
        return raw

    @staticmethod
    def _lookup_message_ids(
        client: IMAPClient,
        message_ids: Iterable[str],
    ) -> tuple[dict[str, int], set[str]]:
        target_ids = set(message_ids)
        if not target_ids:
            return {}, set()
        # Gmail interprets X-GM-RAW exactly like its web search. Braces group
        # the rfc822msgid terms with OR, reducing a batch to one mailbox search.
        query = "{" + " ".join(
            f"rfc822msgid:{message_id}" for message_id in sorted(target_ids)
        ) + "}"
        escaped_query = query.replace("\\", "\\\\").replace('"', '\\"')
        status, response = client.uid(
            "search",
            None,
            "X-GM-RAW",
            f'"{escaped_query}"',
        )
        if status != "OK":
            raise RuntimeError("Gmail X-GM-RAW Message-ID search failed")
        remote_uids = (
            response[0].split()
            if response
            and isinstance(response, list)
            and isinstance(response[0], bytes)
            else []
        )
        matches, _ = ReadOnlyHistoryDownloader._map_message_ids(
            client,
            remote_uids,
            target_ids,
        )
        return matches, target_ids - set(matches)

    def _load_remote_index(
        self,
        path: Path,
        *,
        target_message_ids: set[str],
    ) -> tuple[dict[str, int], set[str], int] | None:
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("folder") != self._active_folder:
                return None
            raw_matches = payload.get("matches")
            if not isinstance(raw_matches, dict):
                return None
            matches = {
                message_id: int(uid)
                for message_id, uid in raw_matches.items()
                if message_id in target_message_ids
            }
            unmatched = {
                str(message_id)
                for message_id in payload.get("unmatched", [])
                if str(message_id) in target_message_ids
            }
            return (
                matches,
                unmatched,
                int(payload.get("exact_message_id_lookups") or 0),
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _write_remote_index(
        self,
        path: Path,
        *,
        matches: dict[str, int],
        unmatched: set[str],
        exact_message_id_lookups: int,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": "rag-imap-remote-index.v2",
                    "folder": self._active_folder,
                    "lookup_strategy": "exact_header_message_id",
                    "exact_message_id_lookups": exact_message_id_lookups,
                    "matches": matches,
                    "unmatched": sorted(unmatched),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def supplement(
        self,
        *,
        raw_dir: Path,
        output_dir: Path,
        report_path: Path | None = None,
        remote_index_path: Path | None = None,
        max_messages: int | None = None,
    ) -> SupplementReport:
        raw_dir = raw_dir.resolve()
        output_dir = output_dir.resolve()
        if not raw_dir.is_dir():
            raise FileNotFoundError(f"raw EML directory does not exist: {raw_dir}")
        if output_dir == raw_dir or output_dir.is_relative_to(raw_dir):
            raise ValueError("output directory must not be the raw directory or inside it")
        if max_messages is not None and max_messages <= 0:
            raise ValueError("max_messages must be positive")

        exports = inspect_local_exports(raw_dir)
        _write_source_map(exports, output_dir=output_dir)
        copied_complete = _copy_existing_complete(exports, output_dir=output_dir)
        missing = [item for item in exports if not item.has_body_payload]
        target_paths: dict[str, list[Path]] = defaultdict(list)
        missing_without_id = 0
        for item in missing:
            target = _output_target(output_dir, item.relative_path)
            if target.exists() and has_body_payload(target.read_bytes()):
                continue
            if item.message_id is None:
                missing_without_id += 1
                continue
            target_paths[item.message_id].append(target)

        client: IMAPClient | None = None
        scanned_message_ids = 0
        matches: dict[str, int] = {}
        known_unmatched: set[str] = set()
        used_cached_remote_index = False
        downloaded_messages = 0
        downloaded_bytes = 0
        stopped_at_limit = False
        stopped_at_message_limit = False
        max_download_bytes = self.settings.max_download_mb * 1024 * 1024
        pending_before_run = len(target_paths)
        remote_uid_count = 0
        try:
            if target_paths:
                client = self._connect()
                cached_index = (
                    self._load_remote_index(
                        remote_index_path,
                        target_message_ids=set(target_paths),
                    )
                    if remote_index_path is not None
                    else None
                )
                if cached_index is not None:
                    matches, known_unmatched, scanned_message_ids = cached_index
                    used_cached_remote_index = True
                    logger.info(
                        "Loaded lookup cache; %s matched and %s unmatched of %s pending",
                        len(matches),
                        len(known_unmatched),
                        len(target_paths),
                    )
                download_limit = max_messages or len(target_paths)
                cached_download_ids = list(matches)[:download_limit]
                lookup_capacity = max(0, download_limit - len(cached_download_ids))
                unknown_ids = sorted(
                    set(target_paths) - set(matches) - known_unmatched
                )
                lookup_ids = unknown_ids[:lookup_capacity]
                if lookup_ids:
                    logger.info(
                        "Running %s exact Message-ID lookups",
                        len(lookup_ids),
                    )
                    new_matches, new_unmatched = self._lookup_message_ids(
                        client,
                        lookup_ids,
                    )
                    matches.update(new_matches)
                    known_unmatched.update(new_unmatched)
                    scanned_message_ids += len(lookup_ids)
                if remote_index_path is not None:
                    self._write_remote_index(
                        remote_index_path,
                        matches=matches,
                        unmatched=known_unmatched,
                        exact_message_id_lookups=scanned_message_ids,
                    )
                download_ids = list(matches)[:download_limit]
                stopped_at_message_limit = (
                    max_messages is not None
                    and len(target_paths) > len(download_ids) + len(known_unmatched)
                )
                for message_id in download_ids:
                    uid = matches[message_id]
                    raw = self._fetch_full_message(client, uid)
                    if downloaded_bytes + len(raw) > max_download_bytes:
                        stopped_at_limit = True
                        break
                    for target in target_paths[message_id]:
                        _atomic_write(target, raw)
                    downloaded_messages += 1
                    downloaded_bytes += len(raw)
                    if downloaded_messages % 25 == 0:
                        logger.info(
                            "Downloaded %s of %s matched messages (%.1f MiB)",
                            downloaded_messages,
                            len(matches),
                            downloaded_bytes / 1024 / 1024,
                        )
        finally:
            self._logout_quietly(client)

        unmatched = tuple(sorted(set(target_paths) & known_unmatched))
        pending_after_run = sum(
            1
            for targets in target_paths.values()
            if any(
                not target.exists() or not has_body_payload(target.read_bytes())
                for target in targets
            )
        )
        output_complete_files = sum(
            1
            for path in output_dir.rglob("*")
            if path.is_file()
            and path.suffix.casefold() == ".eml"
            and has_body_payload(path.read_bytes())
        )
        report = SupplementReport(
            raw_eml_files=len(exports),
            already_complete_local=sum(
                1 for item in exports if item.has_body_payload
            ),
            missing_body_local=len(missing),
            missing_body_with_message_id=sum(
                1 for item in missing if item.message_id is not None
            ),
            missing_body_without_message_id=missing_without_id,
            remote_uids_scanned=(
                remote_uid_count if target_paths else 0
            ),
            remote_message_ids_scanned=scanned_message_ids,
            used_cached_remote_index=used_cached_remote_index,
            matched_message_ids=len(matches),
            unmatched_message_ids=unmatched,
            pending_before_run=pending_before_run,
            pending_after_run=pending_after_run,
            downloaded_messages=downloaded_messages,
            downloaded_bytes=downloaded_bytes,
            copied_complete_files=copied_complete,
            output_complete_files=output_complete_files,
            stopped_at_download_limit=stopped_at_limit,
            stopped_at_message_limit=stopped_at_message_limit,
        )
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return report


def sha256_prefix(payload: bytes, length: int = 12) -> str:
    return hashlib.sha256(payload).hexdigest()[:length]
