"""结构化清洗响应 schema 与不依赖第三方库的严格校验。"""

from __future__ import annotations

import re
from typing import Any

from lecture_ai.config import CleanConfig
from lecture_ai.errors import LLMError

_VISUAL_CUES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("照片/图像", re.compile(r"照片|这[张幅个]?图|图[上中]|看(?:一下)?[^，。！？]{0,10}图")),
    ("板书/黑板", re.compile(r"板书|黑板(?:上)?")),
    ("屏幕/PPT", re.compile(r"屏幕(?:上)?|PPT|幻灯片", re.IGNORECASE)),
    (
        "图示内容",
        re.compile(
            r"(?:这|那|上面|下面|左边|右边)[^，。！？]{0,8}"
            r"(?:波形|电路图|示意图|曲线|表格)"
        ),
    ),
    (
        "公式/板书位置",
        re.compile(
            r"(?:(?:这个|那个|刚才|上面|下面|左边|右边)[^，。！？]{0,10}"
            r"(?:公式|等式|多项式|算式|竖式|位置|小括号)"
            r"|(?:列一个|这个)(?:算式|竖式)"
            r"|写在(?:这个位置|左边|右边|上面|下面)"
            r"|从(?:底下|下面)往上(?:面)?写)"
        ),
    ),
)

_FORMULA_CUE = re.compile(
    r"公式|等式|多项式|次方|乘以|除以|进制|系数|余数|整数|小数|"
    r"(?:^|[^A-Za-z])[KkSsNnMm](?:[_\-\d{]|$)|"
    r"\d[A-Fa-f](?:\.|$)|[A-Fa-f](?:\s*是|乘以)"
)

CLEAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "text": {"type": "string"},
                    "uncertain": {"type": "array", "items": {"type": "string"}},
                    "visual_references": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "corrections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "original": {"type": "string"},
                                "corrected": {"type": "string"},
                                "decision": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["original", "corrected", "decision", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "id", "text", "uncertain", "visual_references", "corrections"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["segments"],
    "additionalProperties": False,
}


def infer_visual_references(text: str) -> list[str]:
    """从原句提取明确视觉线索；只做标注，不改写、不补充课堂事实。"""
    value = str(text or "")
    return [label for label, pattern in _VISUAL_CUES if pattern.search(value)]


_CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CHINESE_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def _parse_chinese_number(value: str) -> str:
    if not any(char in _CHINESE_UNITS for char in value):
        return "".join(str(_CHINESE_DIGITS[char]) for char in value)
    total = 0
    current = 0
    for char in value:
        if char in _CHINESE_DIGITS:
            current = _CHINESE_DIGITS[char]
            continue
        unit = _CHINESE_UNITS[char]
        if unit == 10000:
            total = (total + current) * unit
        else:
            total += (current or 1) * unit
        current = 0
    return str(total + current)


def _numeric_tokens(text: str) -> tuple[str, ...]:
    value = str(text or "")
    value = re.sub(r"十六进制|十进制|八进制|二进制", "", value)
    value = re.sub(r"(?<=\d)\s+(?=\d)", "", value)
    tokens: list[str] = []
    for match in re.finditer(r"\d+|[零〇一二两三四五六七八九十百千万]+", value):
        token = match.group(0)
        if token.isdigit():
            tokens.append(token)
        else:
            tokens.append(_parse_chinese_number(token))
    return tuple(tokens)


def _formula_token_signature(
    text: str,
    *,
    normalize_spoken_numbers: bool,
) -> tuple[tuple[str, ...], str]:
    """提取不能靠上下文猜测的数字/公式字母序列。"""
    value = str(text or "")
    digits = _numeric_tokens(value) if normalize_spoken_numbers else ()
    letters = "".join(re.findall(r"[A-Za-z]", value)).lower()
    return digits, letters


def _validate_formula_token_integrity(source_text: str, cleaned_text: str, segment_id: int) -> None:
    """禁止清洗层改写数值，或在公式语境中增删变量字母。"""
    formula_sensitive = bool(
        _FORMULA_CUE.search(source_text) or _FORMULA_CUE.search(cleaned_text)
    )
    if not formula_sensitive:
        return
    normalize_spoken_numbers = bool(re.search(r"\d", source_text + cleaned_text))
    source_digits, source_letters = _formula_token_signature(
        source_text,
        normalize_spoken_numbers=normalize_spoken_numbers,
    )
    cleaned_digits, cleaned_letters = _formula_token_signature(
        cleaned_text,
        normalize_spoken_numbers=normalize_spoken_numbers,
    )
    if source_digits != cleaned_digits:
        raise LLMError(
            f"segment {segment_id} 改变了数字序列："
            f"source={source_digits!r}, cleaned={cleaned_digits!r}；"
            "公式/数值不得靠上下文补写"
        )
    if source_letters != cleaned_letters:
        raise LLMError(
            f"segment {segment_id} 改变了公式字母序列："
            f"source={source_letters!r}, cleaned={cleaned_letters!r}；"
            "变量和数制符号不得靠上下文补写"
        )


def _repeat_key(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]—…·]+", "", text).lower()


def _remove_cross_segment_duplicate_runs(
    cleaned: list[dict[str, Any]],
    expected: list[dict],
    *,
    threshold: int,
) -> int:
    """连续完全相同的 ASR 短片段只保留首条；返回不计入保留率的源字符数。"""
    ignored_source_chars = 0
    index = 0
    minimum = max(2, int(threshold))
    while index < len(expected):
        source_text = str(expected[index].get("text") or "").strip()
        key = _repeat_key(source_text)
        end = index + 1
        while (
            key
            and end < len(expected)
            and _repeat_key(str(expected[end].get("text") or "")) == key
        ):
            end += 1
        if key and end - index >= minimum:
            reason = f"源转录连续 {end - index} 个 segment 完全重复；保留首条并删除后续副本"
            for offset in range(index + 1, end):
                original = str(expected[offset].get("text") or "").strip()
                ignored_source_chars += len(original)
                cleaned[offset]["text"] = ""
                cleaned[offset]["uncertain"] = list(dict.fromkeys(
                    cleaned[offset]["uncertain"] + [reason]
                ))
                cleaned[offset]["corrections"].append({
                    "original": original,
                    "corrected": "",
                    "decision": "delete",
                    "reason": reason,
                })
        index = end
    return ignored_source_chars


def validate_clean_response(
    data: Any,
    expected: list[dict],
    config: CleanConfig,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict) or set(data) != {"segments"}:
        raise LLMError("LLM JSON 顶层必须且只能包含 segments")
    items = data["segments"]
    if not isinstance(items, list):
        raise LLMError("LLM JSON 的 segments 必须是数组")

    expected_ids = [int(item["id"]) for item in expected]
    actual_ids = [item.get("id") for item in items if isinstance(item, dict)]
    if actual_ids != expected_ids:
        raise LLMError(
            f"LLM 改变了 segment 拓扑：expected={expected_ids}, actual={actual_ids}"
        )

    cleaned: list[dict[str, Any]] = []
    for source, item in zip(expected, items):
        if not isinstance(item, dict) or set(item) != {
            "id", "text", "uncertain", "visual_references", "corrections"
        }:
            raise LLMError("每个清洗 segment 字段必须严格匹配 schema")
        text = item["text"]
        uncertain = item["uncertain"]
        visual = item["visual_references"]
        corrections = item["corrections"]
        if not isinstance(text, str):
            raise LLMError(f"segment {item['id']} text 必须是字符串")
        if not isinstance(uncertain, list) or not all(isinstance(x, str) for x in uncertain):
            raise LLMError(f"segment {item['id']} uncertain 必须是字符串数组")
        if not isinstance(visual, list) or not all(isinstance(x, str) for x in visual):
            raise LLMError(f"segment {item['id']} visual_references 必须是字符串数组")
        correction_keys = {"original", "corrected", "decision", "reason"}
        if not isinstance(corrections, list) or not all(
            isinstance(value, dict)
            and set(value) == correction_keys
            and all(isinstance(value[key], str) for key in correction_keys)
            for value in corrections
        ):
            raise LLMError(f"segment {item['id']} corrections 不符合严格 schema")
        if not text.strip() and not uncertain:
            raise LLMError(
                f"segment {item['id']} 清洗文本为空时必须在 uncertain 记录原因"
            )
        _validate_formula_token_integrity(
            str(source.get("text") or ""),
            text,
            int(item["id"]),
        )
        deduped_corrections: list[dict[str, str]] = []
        seen_corrections: set[tuple[str, str, str, str]] = set()
        for correction in corrections:
            key = tuple(correction[name] for name in (
                "original", "corrected", "decision", "reason"
            ))
            if key not in seen_corrections:
                seen_corrections.add(key)
                deduped_corrections.append(correction)
        source_visual = list(source.get("visual_references") or [])
        inferred_visual = infer_visual_references(str(source.get("text") or ""))
        merged_visual = list(dict.fromkeys(source_visual + inferred_visual + visual))
        cleaned.append(
            {
                "id": int(item["id"]),
                "text": text.strip(),
                "uncertain": uncertain,
                "visual_references": merged_visual,
                "corrections": deduped_corrections,
            }
        )

    ignored_source_chars = _remove_cross_segment_duplicate_runs(
        cleaned,
        expected,
        threshold=config.cross_segment_repetition_threshold,
    )
    before_chars = sum(len(str(item.get("text") or "").strip()) for item in expected)
    effective_before_chars = max(1, before_chars - ignored_source_chars)
    after_chars = sum(len(item["text"]) for item in cleaned)
    ratio = after_chars / effective_before_chars
    if ratio < config.min_retention_ratio:
        raise LLMError(f"清洗结果疑似摘要/删减：字符保留率 {ratio:.3f}")
    if ratio > config.max_expansion_ratio:
        raise LLMError(f"清洗结果疑似扩写：字符膨胀率 {ratio:.3f}")

    for source, output in zip(expected, cleaned):
        original_length = len(str(source.get("text") or "").strip())
        if len(output["text"]) > original_length * 3 + 30:
            raise LLMError(f"segment {output['id']} 疑似注入额外内容")
    return cleaned
