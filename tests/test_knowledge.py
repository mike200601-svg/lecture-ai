"""Phase 2C：双输入硬门、证据路由、视觉未决队列与缓存。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lecture_ai.cli import build_parser
from lecture_ai.errors import LLMError
from lecture_ai.knowledge import KnowledgePipeline, validate_knowledge_response
from lecture_ai.knowledge.schema import _coverage
from lecture_ai.llm import FakeLLMClient
from lecture_ai.structure import StructurePipeline
from tests.test_cleaning import _make_session
from tests.test_structure import _outline, _source, _write_cleaned


def _knowledge() -> dict:
    return {
        "concepts": [{
            "id": "concept_001",
            "topic_id": "topic_001",
            "name": "基本概念",
            "explanation": "老师介绍了基本概念。",
            "importance": 0.9,
            "source_segment_ids": [0, 1],
            "uncertain": [],
        }],
        "equations": [],
        "derivations": [],
        "examples": [{
            "id": "example_001",
            "topic_id": "topic_002",
            "title": "课堂例题",
            "content": "老师讲解了课堂例题。",
            "source_segment_ids": [5, 6],
            "uncertain": [],
        }],
        "teacher_emphasis": [],
        "exam_tips": [],
        "common_errors": [],
        "open_questions": [],
        "visual_references": [],
        "uncertain_items": [],
    }


def _formal_inputs(config, db, *, source: list[dict] | None = None):
    meta, session_dir = _make_session(config, db, repaired=True)
    source = source or _source()
    _write_cleaned(session_dir, meta.session_id, source)
    config.llm.provider = "fake"
    StructurePipeline(
        config,
        db,
        client=FakeLLMClient(lambda _: _outline(source)),
        sleep=lambda _: None,
    ).run(meta.session_id)
    outline = json.loads(
        (session_dir / "analysis" / "outline.json").read_text(encoding="utf-8")
    )
    return meta, session_dir, source, outline


def test_knowledge_validator_accepts_traceable_result(config, db):
    _, _, source, outline = _formal_inputs(config, db)
    result = validate_knowledge_response(
        _knowledge(), source, outline, concept_threshold=0.8
    )
    assert result["concepts"][0]["importance"] == 0.9
    assert result["examples"][0]["source_segment_ids"] == [5, 6]


def test_knowledge_rejects_low_importance_unknown_source_and_lost_outline(config, db):
    _, _, source, outline = _formal_inputs(config, db)
    low = _knowledge()
    low["concepts"][0]["importance"] = 0.4
    with pytest.raises(LLMError, match="低于阈值"):
        validate_knowledge_response(low, source, outline, concept_threshold=0.8)

    unknown = _knowledge()
    unknown["concepts"][0]["source_segment_ids"] = [999]
    with pytest.raises(LLMError, match="未知 segment"):
        validate_knowledge_response(unknown, source, outline, concept_threshold=0.8)

    lost = _knowledge()
    lost["examples"] = []
    with pytest.raises(LLMError, match="examples.*丢失"):
        validate_knowledge_response(lost, source, outline, concept_threshold=0.8)


def test_knowledge_routes_uncertainty_and_visual_reference(config, db):
    source = _source()
    source[2]["uncertain"] = ["术语听不清"]
    source[2]["visual_references"] = ["板书/黑板"]
    _, _, source, outline = _formal_inputs(config, db, source=source)
    response = _knowledge()
    with pytest.raises(LLMError, match="uncertainty"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    response["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "相关术语听不清", "reason": "CLEANED 已标记不确定",
        "source_segment_ids": [2],
    }]
    with pytest.raises(LLMError, match="visual reference"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    response["visual_references"] = [{
        "id": "visual_001", "topic_id": "topic_001", "timestamp": 120.0,
        "context": "老师指向板书", "reference_type": "board", "confidence": 0.8,
        "source_segment_ids": [2], "uncertain": [],
    }]
    accepted = validate_knowledge_response(
        response, source, outline, concept_threshold=0.8
    )
    assert accepted["visual_references"][0]["timestamp"] == 120.0


def test_knowledge_rejects_fabricated_and_unrouted_incomplete_equation(config, db):
    source = _source()
    for item in source:
        item["text"] = "普通课堂内容"
    _, _, source, outline = _formal_inputs(config, db, source=source)
    response = _knowledge()
    response["equations"] = [{
        "id": "equation_001", "topic_id": "topic_001", "name": "凭空公式",
        "expression": "x=42", "status": "complete", "source_segment_ids": [2],
        "uncertain": [],
    }]
    with pytest.raises(LLMError, match="疑似编造"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    source[2]["text"] = "这个公式等于什么听不清。"
    response["equations"][0]["status"] = "incomplete"
    response["equations"][0]["uncertain"] = ["表达式不完整"]
    with pytest.raises(LLMError, match="不完整公式未进入"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)


def test_knowledge_requires_cleaned_and_outline(config, db):
    meta, session_dir = _make_session(config, db, repaired=True)
    with pytest.raises(LLMError, match="缺少正式 CLEANED"):
        KnowledgePipeline(config, db).run(meta.session_id, dry_run=True)
    _write_cleaned(session_dir, meta.session_id)
    with pytest.raises(LLMError, match="缺少正式 STRUCTURED"):
        KnowledgePipeline(config, db).run(meta.session_id, dry_run=True)


def test_knowledge_pipeline_writes_both_outputs_and_reuses(config, db):
    meta, session_dir, _, _ = _formal_inputs(config, db)
    fake = FakeLLMClient(lambda _: _knowledge())
    pipeline = KnowledgePipeline(config, db, client=fake, sleep=lambda _: None)

    first = pipeline.run(meta.session_id)
    second = pipeline.run(meta.session_id)

    assert first.output_json and first.unresolved_visual_json
    assert second.reused and fake.calls == 1
    knowledge = json.loads(Path(first.output_json).read_text(encoding="utf-8"))
    unresolved = json.loads(
        Path(first.unresolved_visual_json).read_text(encoding="utf-8")
    )
    assert knowledge["layer"] == "KNOWLEDGE"
    assert knowledge["source"]["segment_count"] == 10
    assert unresolved["layer"] == "UNRESOLVED_VISUAL"
    assert unresolved["item_count"] == 0
    assert pipeline.sessions.load(meta.session_id).steps["knowledge"].status == "done"


def test_knowledge_web_prepares_and_rejects_invalid_response(config, db):
    meta, session_dir, _, _ = _formal_inputs(config, db)
    config.llm.provider = "chatgpt_web"
    config.llm.model = "chatgpt-web-high"
    config.privacy.allow_cloud_transcript = True
    pipeline = KnowledgePipeline(config, db, sleep=lambda _: None)

    pending = pipeline.run(meta.session_id)
    exchange = session_dir / "analysis" / "knowledge_web" / "extract"
    assert pending.partial and (exchange / "prompt.md").exists()
    request = json.loads((exchange / "request.json").read_text(encoding="utf-8"))
    assert request["pipeline"] == "knowledge"
    (exchange / "response.json").write_text('{"concepts": []}', encoding="utf-8")

    retry = pipeline.run(meta.session_id)
    assert retry.partial
    assert len(list(exchange.glob("response.rejected.*.json"))) == 1
    assert pipeline.sessions.load(meta.session_id).steps["knowledge"].status == "pending"


def test_knowledge_cli_supports_dry_run_and_force():
    args = build_parser().parse_args(["knowledge", "session-1", "--dry-run", "--force"])
    assert args.command == "knowledge"
    assert args.session_id == "session-1" and args.dry_run and args.force


def test_knowledge_accepts_blank_uncertain_content_only_for_audited_blank_source(
    config, db
):
    """2A 审计清空的 segment 无正文可引，uncertain_items 允许空 content。

    真实 Gold 回归：CLEANED 把术语串入/模板幻觉/复读副本清空后，Phase 2C 仍必须为
    这些 segment 留下可追溯的 uncertain 条目；要求非空 content 等于逼模型编内容。
    非空来源仍然不许空 content。
    """
    source = _source()
    source[2]["text"] = ""
    source[2]["uncertain"] = ["整段为术语表串入，已在 CLEANED 审计删除"]
    _, _, source, outline = _formal_inputs(config, db, source=source)

    response = _knowledge()
    response["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "", "reason": "整段在 CLEANED 中已被审计删除，无正文可引用",
        "source_segment_ids": [2],
    }]
    accepted = validate_knowledge_response(
        response, source, outline, concept_threshold=0.8
    )
    assert accepted["uncertain_items"][0]["content"] == ""
    assert accepted["uncertain_items"][0]["source_segment_ids"] == [2]

    # 来源非空时空 content 仍然拒绝
    still_strict = _knowledge()
    still_strict["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "", "reason": "偷懒留空",
        "source_segment_ids": [0, 1],
    }]
    with pytest.raises(LLMError, match="content 不能为空"):
        validate_knowledge_response(
            still_strict, source, outline, concept_threshold=0.8
        )

    # reason 任何时候都不能为空
    no_reason = _knowledge()
    no_reason["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "", "reason": "",
        "source_segment_ids": [2],
    }]
    with pytest.raises(LLMError, match="reason 不能为空"):
        validate_knowledge_response(
            no_reason, source, outline, concept_threshold=0.8
        )


def test_knowledge_incomplete_equation_needs_uncertainty_but_not_full_span(config, db):
    """不完整公式要求命中问题段，而不是整条证据区间都标存疑。

    真实 Gold 回归：基数乘法推导跨 12 个 segment，只有一处口述不完整。要求全覆盖
    会把清晰语音也拖进 uncertainty，Phase 2D 再把每条渲染成 [!question]，等于制造疑点。
    """
    source = _source()
    for item in source:
        item["text"] = "老师板书这个式子等于多少。"
    _, _, source, outline = _formal_inputs(config, db, source=source)

    response = _knowledge()
    response["equations"] = [{
        "id": "equation_001", "topic_id": "topic_001", "name": "跨多段的推导",
        "expression": "S = K_n·2^n + …", "status": "incomplete",
        "source_segment_ids": [0, 1, 2, 3], "uncertain": ["第2段口述不完整"],
    }]

    # 零覆盖仍然拒绝
    with pytest.raises(LLMError, match="不完整公式未进入"):
        validate_knowledge_response(response, source, outline, concept_threshold=0.8)

    # 只覆盖真正有问题的那一段即可通过
    response["uncertain_items"] = [{
        "id": "uncertain_001", "topic_id": "topic_001",
        "content": "这个式子等于多少没说完", "reason": "口述不完整，依赖板书",
        "source_segment_ids": [2],
    }]
    accepted = validate_knowledge_response(
        response, source, outline, concept_threshold=0.8
    )
    assert accepted["equations"][0]["status"] == "incomplete"
    assert _coverage(accepted["uncertain_items"]) == {2}


def _derivation(**overrides) -> dict:
    item = {
        "id": "derivation_001",
        "topic_id": "topic_001",
        "name": "基数除法原理",
        "steps": [
            "把 S 的前 n 项各提出一个 2，右边只剩 K_0 乘以 2 的 0 次方。",
            "2 的 0 次方等于 1，所以 S 除以 2 的余数就是 K_0。",
        ],
        "conclusion": "连续除以 2 取余，依次得到 K_0、K_1 直到 K_n。",
        "status": "complete",
        "source_segment_ids": [0, 1],
        "uncertain": [],
    }
    item.update(overrides)
    return item


def _numeric_source() -> list[dict]:
    """推导校验要求来源里有公式/计算证据，默认 fixture 的纯文字不满足。"""
    source = _source()
    for item in source[:4]:
        item["text"] = f"第{item['id']}段：把 S 除以 2 得到余数 K_0 等于 1。"
    return source


def test_knowledge_accepts_derivation_steps_and_keeps_them_separate_from_equations():
    """2C 必须把老师的推导过程本身留下来，而不是只留结论。

    A/B 对照发现 outline 检出的 derivations 在 2B→2C 边界被整体丢弃，笔记里
    只剩一行结论。这条测试锁住修复后的行为。
    """
    source = _numeric_source()
    response = _knowledge()
    response["derivations"] = [_derivation()]
    accepted = validate_knowledge_response(
        response, source, _outline(source), concept_threshold=0.8
    )
    assert [item["id"] for item in accepted["derivations"]] == ["derivation_001"]
    assert len(accepted["derivations"][0]["steps"]) == 2
    assert accepted["derivations"][0]["conclusion"].startswith("连续除以 2")
    assert accepted["equations"] == []


def test_knowledge_rejects_single_step_derivation_as_conclusion_only():
    source = _numeric_source()
    response = _knowledge()
    response["derivations"] = [_derivation(steps=["连续除以 2 取余即可。"])]
    with pytest.raises(LLMError, match="至少给出 2 个推导步骤"):
        validate_knowledge_response(
            response, source, _outline(source), concept_threshold=0.8
        )


def test_knowledge_rejects_derivation_without_calculation_evidence():
    # _FORMULA_CUE 会命中任意数字，默认 fixture 的「第0段课堂内容」本身就算证据，
    # 这里显式换成完全没有计算线索的文本。
    source = _source()
    for item in source:
        item["text"] = "老师在讲台上随口聊了几句与本节无关的闲话。"
    response = _knowledge()
    response["derivations"] = [_derivation()]
    with pytest.raises(LLMError, match="没有推导/计算证据"):
        validate_knowledge_response(
            response, source, _outline(source), concept_threshold=0.8
        )


def test_knowledge_rejects_incomplete_derivation_without_uncertainty():
    source = _numeric_source()
    response = _knowledge()
    response["derivations"] = [_derivation(status="incomplete")]
    with pytest.raises(LLMError, match="不完整时必须说明 uncertainty"):
        validate_knowledge_response(
            response, source, _outline(source), concept_threshold=0.8
        )


def test_outline_derivation_must_be_carried_by_knowledge_derivations():
    """旧实现允许用 equations 的证据区间顶替推导，这正是推理丢失的根因。"""
    source = _numeric_source()
    outline = _outline(source)
    outline["derivations"] = [{
        "id": "R01",
        "topic_id": "topic_001",
        "label": "基数除法原理",
        "start": source[0]["start"],
        "end": source[1]["end"],
        "source_segment_ids": [0, 1],
        "uncertain": [],
    }]
    response = _knowledge()
    response["equations"] = [{
        "id": "equation_001",
        "topic_id": "topic_001",
        "name": "基数除法取位关系",
        "expression": "S 除以 2 的余数为 K_0",
        "status": "complete",
        "source_segment_ids": [0, 1],
        "uncertain": [],
    }]
    with pytest.raises(LLMError, match="outline derivations 在知识层丢失来源"):
        validate_knowledge_response(
            response, source, outline, concept_threshold=0.8
        )
    response["derivations"] = [_derivation()]
    accepted = validate_knowledge_response(
        response, source, outline, concept_threshold=0.8
    )
    assert accepted["derivations"][0]["id"] == "derivation_001"


def test_legacy_v1_knowledge_without_derivations_still_loads():
    """现有 Gold 是 v1 产物，没有 derivations；加了新类别后必须仍能读取。"""
    source = _numeric_source()
    outline = _outline(source)
    outline["derivations"] = [{
        "id": "R01",
        "topic_id": "topic_001",
        "label": "基数除法原理",
        "start": source[0]["start"],
        "end": source[1]["end"],
        "source_segment_ids": [0, 1],
        "uncertain": [],
    }]
    legacy = _knowledge()
    del legacy["derivations"]
    legacy["equations"] = [{
        "id": "equation_001",
        "topic_id": "topic_001",
        "name": "基数除法取位关系",
        "expression": "S 除以 2 的余数为 K_0",
        "status": "complete",
        "source_segment_ids": [0, 1],
        "uncertain": [],
    }]
    accepted = validate_knowledge_response(
        legacy, source, outline, concept_threshold=0.8
    )
    assert accepted["derivations"] == []
