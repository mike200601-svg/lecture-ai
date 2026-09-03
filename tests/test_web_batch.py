"""GPT 网页批处理：整包导出、手机交换、严格回收与自动续跑。"""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from lecture_ai.cleaning.web_batch import CleanWebBatchService
from lecture_ai.errors import LLMError
from tests.test_cleaning import _faithful_responder, _make_session
from tests.test_structure import _outline, _source, _write_cleaned


def _return_directory(
    package_dir: Path,
    target: Path,
    *,
    invalid: set[str] | None = None,
    responder=_faithful_responder,
) -> Path:
    invalid = invalid or set()
    target.mkdir(parents=True)
    shutil.copy2(package_dir / "manifest.json", target / "manifest.json")
    manifest = json.loads((package_dir / "manifest.json").read_text(encoding="utf-8"))
    for task in manifest["tasks"]:
        task_id = task["task_id"]
        response = target / task["response_file"]
        response.parent.mkdir(parents=True, exist_ok=True)
        if task_id in invalid:
            payload = {"segments": []}
        else:
            prompt = (package_dir / task["prompt_file"]).read_text(encoding="utf-8")
            payload = responder(prompt)
        response.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


def test_batch_prepare_is_idempotent_and_uses_phone_exchange(config, db):
    meta, _ = _make_session(config, db, repaired=True)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    service = CleanWebBatchService(config, db)

    first = service.prepare(meta.session_id)
    package_zip = Path(first.package_zip)
    first_mtime = package_zip.stat().st_mtime_ns
    second = service.prepare(meta.session_id)

    assert first.status == "waiting_for_web"
    assert first.task_ids == ["chunk_000", "chunk_001"]
    assert package_zip.parent == config.paths.web_exchange / meta.session_id / "to_phone"
    assert second.package_zip == first.package_zip
    assert package_zip.stat().st_mtime_ns == first_mtime
    with zipfile.ZipFile(package_zip) as archive:
        names = set(archive.namelist())
    assert "manifest.json" in names
    assert "README.md" in names
    assert "tasks/chunk_000/prompt.md" in names
    assert "tasks/chunk_001/schema.json" in names
    state = json.loads(
        (config.paths.web_exchange / meta.session_id / "state.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["status"] == "waiting_for_web"


def test_batch_receive_validates_every_result_and_assembles(config, db, tmp_path):
    meta, session_dir = _make_session(config, db, repaired=True)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    service = CleanWebBatchService(config, db)
    prepared = service.prepare(meta.session_id)
    returned = _return_directory(Path(prepared.package_dir), tmp_path / "returned")

    outcome = service.receive(meta.session_id, returned)

    assert outcome.status == "ready_for_phase2a_qa"
    assert outcome.accepted == 2 and outcome.rejected == 0
    assert (session_dir / "analysis" / "transcript_clean.json").exists()
    assert Path(outcome.output_json).exists()


def test_batch_receive_isolates_bad_item_and_prepares_retry(config, db, tmp_path):
    meta, session_dir = _make_session(config, db, repaired=True)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    service = CleanWebBatchService(config, db)
    prepared = service.prepare(meta.session_id)
    returned = _return_directory(
        Path(prepared.package_dir), tmp_path / "returned", invalid={"chunk_001"}
    )

    outcome = service.receive(meta.session_id, returned)

    assert outcome.status == "waiting_for_web"
    assert outcome.accepted == 1 and outcome.rejected == 1
    assert outcome.task_ids == ["chunk_001"]
    assert (session_dir / "analysis" / "clean_cache" / "chunk_000.json").exists()
    assert not (session_dir / "analysis" / "clean_cache" / "chunk_001.json").exists()
    rejected = session_dir / "analysis" / "clean_web" / "chunk_001"
    assert len(list(rejected.glob("response.rejected.*.json"))) == 1
    assert Path(outcome.package_zip).exists()


def test_batch_rejects_tampered_manifest(config, db, tmp_path):
    meta, _ = _make_session(config, db, repaired=True)
    config.llm.provider = "chatgpt_web"
    config.privacy.allow_cloud_transcript = True
    service = CleanWebBatchService(config, db)
    prepared = service.prepare(meta.session_id)
    returned = _return_directory(Path(prepared.package_dir), tmp_path / "returned")
    manifest_path = returned / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tasks"][0]["prompt_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(LLMError, match="manifest"):
        service.receive(meta.session_id, returned)


def test_structure_task_uses_same_phone_batch_and_auto_resumes(config, db, tmp_path):
    meta, session_dir = _make_session(config, db, repaired=True)
    source = _source()
    _write_cleaned(session_dir, meta.session_id, source)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    service = CleanWebBatchService(config, db)

    prepared = service.prepare_structure(meta.session_id)
    returned = _return_directory(
        Path(prepared.package_dir),
        tmp_path / "returned_structure",
        responder=lambda _: _outline(source),
    )
    outcome = service.receive(meta.session_id, returned)

    assert prepared.task_ids == ["structure_outline"]
    assert outcome.status == "ready_for_phase2b_qa"
    assert outcome.accepted == 1 and outcome.rejected == 0
    assert (session_dir / "analysis" / "outline.json").exists()
