import json
import zipfile
from pathlib import Path

from app.nas_knowledge import (
    Classification,
    LocalKnowledgeBase,
    NASKnowledgeScanner,
    set_classification_override,
)


def _write_policy(path: Path) -> None:
    path.write_text(
        """
version: 1
rules:
  - pattern: "public/**"
    classification: customer_candidate
    reason: public candidate
  - pattern: "internal/**"
    classification: internal
    reason: internal
  - pattern: "**"
    classification: review_required
    reason: unknown
excluded_extensions: [.exe]
extractable_extensions: [.docx, .txt]
sensitive_markers:
  - "(?i)unit price"
""".strip(),
        encoding="utf-8",
    )


def _write_docx(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def test_customer_index_is_deny_by_default_and_sensitive_candidates_are_held(tmp_path: Path) -> None:
    root = tmp_path / "nas"
    output = tmp_path / "index"
    policy = tmp_path / "policy.yaml"
    _write_policy(policy)
    _write_docx(root / "public" / "brochure.docx", "Silane coupling agent technical data")
    _write_docx(root / "public" / "quote.docx", "Unit price: USD 5.00")
    (root / "internal").mkdir(parents=True)
    (root / "internal" / "notes.txt").write_text("private formulation notes", encoding="utf-8")

    summary = NASKnowledgeScanner(root=root, policy_path=policy, output_dir=output).scan()

    assert summary["classification_counts"] == {
        "customer_ready": 1,
        "internal": 1,
        "review_required": 1,
    }
    knowledge = LocalKnowledgeBase(output / "knowledge_index.json")
    assert [match.path for match in knowledge.search("silane", audience="customer")] == [
        "public/brochure.docx"
    ]
    assert knowledge.search("price", audience="customer") == ()
    assert [match.path for match in knowledge.search("formulation", audience="internal")] == [
        "internal/notes.txt"
    ]


def test_incremental_scan_removes_deleted_files_and_preserves_manual_override(tmp_path: Path) -> None:
    root = tmp_path / "nas"
    output = tmp_path / "index"
    policy = tmp_path / "policy.yaml"
    _write_policy(policy)
    source = root / "public" / "brochure.docx"
    _write_docx(source, "Product brochure")
    scanner = NASKnowledgeScanner(root=root, policy_path=policy, output_dir=output)
    scanner.scan()
    set_classification_override(
        output_dir=output,
        relative_path="public/brochure.docx",
        classification=Classification.INTERNAL,
        reason="contains unreleased product",
        actor="tester",
    )

    scanner.scan()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["documents"][0]["classification"] == "internal"
    assert manifest["documents"][0]["classification_source"].startswith("manual override")

    source.unlink()
    summary = scanner.scan()
    assert summary["file_count"] == 0
    assert LocalKnowledgeBase(output / "knowledge_index.json").search("product", audience="internal") == ()
