"""Offline, evidence-based acceptance checks for generated lecture notes.

This deliberately does not assign a prose-quality score. It detects a small
set of reproducible omissions and contradictions, and labels missing evidence
as unverified so a release cannot silently pass on incomplete inputs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


_AMBIGUOUS_RAC = re.compile(r"(?<![A-Za-z])rac\s*算法", re.I)
_PUBLIC_KEY_CONTEXT = re.compile(
    r"公钥|私钥|非对称|Rivest|Shamir|Adleman|RSA", re.I
)


def ambiguous_public_key_segment(segments: list[dict]) -> tuple[int, dict] | None:
    """Find RAC ASR only near public-key discussion, not an unrelated RAC term."""
    def at(row: dict) -> float | None:
        try:
            return float(row["start"])
        except (TypeError, ValueError):
            return None
        except KeyError:
            return None
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict) or not _AMBIGUOUS_RAC.search(str(segment.get("text") or "")):
            continue
        start = at(segment)
        if start is None:
            continue
        context = " ".join(str(row.get("text") or "") for row in segments
                           if isinstance(row, dict) and at(row) is not None
                           and abs(at(row) - start) <= 35)
        if _PUBLIC_KEY_CONTEXT.search(context):
            return index, segment
    return None


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def review_fingerprint(note: str, transcript: dict, *, sources: list[dict] | None,
                       assignments: list[dict] | None,
                       assignment_catalog_complete: bool,
                       published_blocks: list[dict] | None,
                       attendance_items: list[str] | None,
                       required_claims: list[dict] | None) -> str:
    """Bind editorial signoff to the exact note and evidence it reviewed."""
    payload = {
        "note": note, "transcript": transcript, "sources": sources,
        "assignments": assignments,
        "assignment_catalog_complete": assignment_catalog_complete,
        "published_blocks": published_blocks,
        "attendance_items": attendance_items,
        "required_claims": required_claims,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stamp_seconds(minutes: str, seconds: str) -> int:
    return int(minutes) * 60 + int(seconds)


def _published_content(blocks: list[dict] | None) -> tuple[str, str]:
    lines: list[str] = []
    links: list[str] = []

    def visit(block: dict) -> None:
        kind = block.get("type")
        body = block.get(kind, {}) if isinstance(kind, str) else {}
        for part in body.get("rich_text", []) if isinstance(body, dict) else []:
            if not isinstance(part, dict):
                continue
            lines.append(str(part.get("plain_text") or (part.get("text") or {}).get("content") or ""))
            link = (part.get("text") or {}).get("link") or {}
            url = part.get("href") or link.get("url")
            if url:
                links.append(unquote(str(url)))
        for child in block.get("children", []):
            if isinstance(child, dict):
                visit(child)

    for block in blocks or []:
        if isinstance(block, dict):
            visit(block)
    return "\n".join(lines), "\n".join(links)


def _assignment_link_present(links: str, item: dict) -> bool:
    """Require a submission URL for this exact Blackboard course and item."""
    content_id = str(item.get("content_id") or "")
    course_id = str(item.get("course_id") or "")
    for raw in links.splitlines():
        parsed = _campus_https_url(raw)
        if parsed is None or parsed.path != "/webapps/assignment/uploadAssignment":
            continue
        query = parse_qs(parsed.query)
        if query.get("content_id") != [content_id]:
            continue
        if course_id and query.get("course_id") != [course_id]:
            continue
        return True
    return False


def _campus_https_url(raw: str):
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or parsed.hostname != "course.pku.edu.cn"
                or parsed.port not in (None, 443) or parsed.username or parsed.password
                or parsed.fragment):
            return None
        return parsed
    except ValueError:
        return None


def _attachment_link_present(links: str, path: str) -> bool:
    """Reject lookalike filenames and paths belonging to another attachment."""
    expected = urlsplit(path)
    expected_path = unquote(expected.path)
    if not expected_path.startswith("/bbcswebdav/") or ".." in expected_path.split("/"):
        return False
    return any(
        (parsed := _campus_https_url(raw)) is not None
        and unquote(parsed.path) == expected_path
        for raw in links.splitlines()
    )


def note_factual_conflicts(note: str, sources: list[dict] | None = None) -> list[str]:
    """Catch observed hard errors that page-existence checks cannot detect.

    This is a small fail-closed check, not a claim that all prose has been
    semantically verified. A human still reviews the remaining statements.
    """
    body = note.split("## 课堂内容", 1)[-1].split("## 作业与考试口头线索", 1)[0]
    pages = {(str(row.get("title") or ""), str(row.get("locator") or "")):
             str(row.get("text") or "") for row in sources or []
             if row.get("status") == "readable"}
    issues: list[str] = []
    for paragraph in re.split(r"\n\s*\n", body):
        for line in paragraph.splitlines():
            for citation in re.finditer(
                    r"（课件：([^，（）]+)，第\s*(\d+)\s*页(?:；[^）]*)?）", line):
                clauses = [part.strip() for part in re.split(r"[。！？]", line[:citation.start()])
                           if part.strip()]
                if not clauses or not re.search(r"(?<![A-Za-z])RSA(?![A-Za-z])", clauses[-1], re.I):
                    continue
                title, page_number = citation.groups()
                page = pages.get((title.strip(), f"第 {int(page_number)} 页"))
                if page is not None and not re.search(r"(?<![A-Za-z])RSA(?![A-Za-z])", page, re.I):
                    issues.append(f"RSA 论断错引 {title.strip()} 第 {page_number} 页；该页未提 RSA")
        # The five-tuple slide defines an algorithm for each key; the key
        # selects that function. It does not let a reader derive the cipher.
        cipher_page = pages.get(("ch02-古典密码.pdf", "第 11 页"), "")
        if ("密码体制" in cipher_page
                and re.search(r"密钥.{0,4}(?:是|用于).{0,4}推导(?:加解密)?算法", paragraph)):
            issues.append("把密钥选择或控制已有算法误写成密钥可推导算法")
        if (re.search(r"(?:军事|军用).{0,45}算法保密", paragraph)
                and re.search(r"(?:确保|保证|必然).{0,10}(?:更高|更强|较高|高级).{0,8}安全", paragraph)):
            issues.append("把军事算法保密策略误写成更高安全性的保证")
        if (re.search(r"(?:凯撒|移位|古典密码).{0,35}因.{0,24}未公开", paragraph)
                and re.search(r"未公开.{0,18}(?:易|容易|极易).{0,8}(?:被)?穷举", paragraph)):
            issues.append("把算法未公开误写成容易穷举的原因；穷举来自密钥空间小")
        cryptology_page = pages.get(("ch02-古典密码.pdf", "第 5 页"), "")
        if ("密码学" in cryptology_page and "密码编码学" in cryptology_page
                and "密码分析学" in cryptology_page
                and re.search(r"(?:对称密码|对称加密)(?:算法|体制)?"
                              r".{0,12}(?:分为|包含|有).{0,15}(?:密码)?编码"
                              r".{0,12}(?:密码)?分析", paragraph)):
            issues.append("把密码学的编码与分析两分支误归到对称密码")
        polyalphabetic_page = pages.get(("ch02-古典密码.pdf", "第 38 页"), "")
        if ("多字母密码" in polyalphabetic_page and "上下文" in polyalphabetic_page
                and re.search(r"多字母密码.{0,55}(?:相当于.{0,12}分组密码|"
                              r"每次(?:统一)?处理多个字母)", paragraph)):
            issues.append("把课件第 38 页按位置变换的多字母密码误写成多字符分组处理")
        otp_page = pages.get(("ch02-古典密码.pdf", "第 33 页"), "")
        if (re.search(r"One-time pad|一次一密", otp_page, re.I)
                and "一次一密" in paragraph
                and re.search(r"(?:无条件安全|一次一密).{0,20}"
                              r"(?:仅|只|完全)存在于理论(?:层面|上)", paragraph)):
            issues.append("把一次一密的理论安全保证误写成一次一密仅存在于理论、从未实际使用")
        byte_page = pages.get(("ch02-古典密码.pdf", "第 36 页"))
        if (byte_page is not None and "字节" not in byte_page
                and "字符" in byte_page
                and re.search(
                    r"现代密码.{0,10}(?:通常|一般|大多|主要).{0,14}"
                    r"(?:按字节(?:进行)?处理|以字节为单位(?:进行)?处理)",
                    paragraph,
                )
                and re.search(r"ch02-古典密码\.pdf\s*，\s*第\s*36\s*页", paragraph)):
            issues.append("用仅讲字符代替和置换的课件第 36 页支持现代密码通常按字节处理")
        # The slide's 11^7 example needs the 4+2+1 powers. Repeated
        # squaring alone only produces powers of two, never the odd exponent.
        superscripts = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
        normalized_math = re.sub(
            r"11\s*([⁰¹²³⁴⁵⁶⁷⁸⁹]+)",
            lambda match: "11^" + match.group(1).translate(superscripts), paragraph,
        )
        if (re.search(r"(?<!\d)11\s*\^\s*7(?!\d)", normalized_math)
                and re.search(r"平方.{0,80}(?:再|再次|然后|接着).{0,20}平方", paragraph)
                and not (re.search(r"7\s*=\s*4\s*\+\s*2\s*\+\s*1", normalized_math)
                         or (re.search(r"(?<!\d)11\s*\^\s*4(?!\d)", normalized_math)
                             and re.search(r"乘|[×*·]|\\times\b|\\cdot\b", normalized_math)))):
            issues.append("11^7 的平方取模示例只写连续平方；还需按 7=4+2+1 将 11、11^2、11^4 相乘")
        if (re.search(r"卡契斯基|Kasiski", paragraph, re.I)
                and re.search(r"(?:重复|长).{0,8}(?:字符|字母)(?:串|片段).{0,24}(?:4|四).{0,6}(?:字母|字符).{0,4}以上", paragraph)):
            issues.append("卡契斯基分析把课件中大于四字母的密钥候选长度误写成重复串长度")
        if (re.search(r"卡契斯基|Kasiski", paragraph, re.I)
                and re.search(r"[4四]\s*字母以上.{0,20}重复(?:字符|字母)串长度限制", paragraph)):
            issues.append("卡契斯基四字母条件被明确误归到重复串长度")
        if (re.search(r"卡契斯基|Kasiski", paragraph, re.I)
                and re.search(r"长串\s*（通常(?:考虑|要求)?\s*[4四]\s*(?:个)?字母以上）", paragraph)):
            issues.append("卡契斯基把四字母以上误写为长串的限制")
        # The table on this page makes extraction status "partial", but the
        # qualifying sentence itself is complete and safe to compare exactly.
        kasiski_page = next((str(row.get("text") or "") for row in sources or []
                             if row.get("title") == "ch02-古典密码.pdf"
                             and row.get("locator") == "第 119 页"
                             and row.get("status") in {"readable", "partial"}), "")
        if (re.search(r"只考虑\s*4\s*个字母以上的密钥长度", kasiski_page)
                and re.search(r"卡契斯基|Kasiski", paragraph, re.I)
                and re.search(r"(?:只考虑|仅考虑|通常考虑|要求)\s*[4四]\s*(?:个)?"
                              r"字母以上的重复(?:字母|字符)?串", paragraph)):
            issues.append("卡契斯基把课件第 119 页的密钥长度条件误写为重复串长度")
        cipher_types = "\n".join(pages.get(("ch02-古典密码.pdf", f"第 {number} 页"), "")
                                  for number in (132, 133))
        if ("弗纳" in cipher_types and "转轮" in cipher_types
                and re.search(r"(?:弗?纳姆|Vernam)密码转轮(?:密码)?机", paragraph, re.I)):
            issues.append("把弗纳姆密码和转轮密码机误合并为一个术语")
        if re.search(r"模运算.{0,6}(?:满足|遵循).{0,4}分配律", paragraph):
            issues.append("把先取模再运算的同余性质误称为分配律")
        if (re.search(r"Hill|希尔密码", paragraph, re.I)
                and "明密文对" in paragraph and "即可" in paragraph
                and re.search(r"(?:还原|求出|恢复).{0,12}(?:矩阵|密钥)", paragraph)
                and "可逆" not in paragraph and "逆矩阵存在" not in paragraph):
            issues.append("Hill 已知明文求密钥省略明文矩阵可逆的前提")
        history_sources = "\n".join(
            content for (title, locator), content in pages.items()
            if "古典密码" in title and locator in {"第 29 页", "第 30 页"})
        if ("大卫·卡恩" in paragraph and "1975" in paragraph
                and re.search(r"1967.{0,15}David Kahn", history_sources)):
            issues.append("把课件中 David Kahn 1967 年的著作误写为 1975 年")
        if ("香农" in paragraph and "卡恩" in paragraph
                and re.search(r"他们.{0,100}加密.{0,30}解密.{0,40}不同的密钥", paragraph)
                and "1976" in history_sources and "1977" in history_sources):
            issues.append("把香农和卡恩与 1976–1977 年公钥密码作者混为同一主语")
        if ("RSA" in paragraph and re.search(r"次年.{0,30}三位作者", paragraph)
                and "1976" in history_sources and "1977" in history_sources
                and not re.search(r"Rivest.{0,30}Shamir.{0,30}Adleman", paragraph, re.I)):
            issues.append("RSA 段落用含混的“次年三位作者”代替课件明确的 1977 年作者")
        if (re.search(r"(?:IC|重合指数)", paragraph, re.I)
                and re.search(r"(?:证实|证明|确认).{0,12}(?:周期|密钥长度)|"
                              r"(?:周期|密钥长度).{0,12}(?:已被证实|得到证明)", paragraph)):
            issues.append("把重合指数对密钥长度猜测的支持误写成证明或确认")
        # Markdown lists often put the symmetric and asymmetric definitions in
        # adjacent items without a blank line. Judge each item independently.
        for item in re.split(r"(?m)(?=^\s*(?:[-*+]|\d+[.)])\s+)", paragraph):
            public_key = re.search(r"其中.{0,15}(?:加密)?密钥.{0,8}公开.{0,30}公钥", item)
            topic_before_key = (list(re.finditer(r"非对称|(?<!非)对称", item[:public_key.start()]))
                                if public_key else [])
            if (public_key
                    and re.search(r"(?<!非)对称.{0,45}(?:密钥相同|单密钥|秘密密钥)", item)
                    and topic_before_key
                    and topic_before_key[-1].group() == "对称"):
                issues.append("把公钥私钥的说明接在对称密码下，混淆两种体制")
        for term, aliases in (("Playfair", ("Playfair", "普莱费尔")),
                              ("Hill", ("Hill", "希尔密码"))):
            positions = [paragraph.casefold().find(alias.casefold()) for alias in aliases]
            positions = [position for position in positions if position >= 0]
            if not positions:
                continue
            start = min(positions)
            next_pattern = (r"Hill|希尔密码" if term == "Playfair"
                            else r"Playfair|普莱费尔")
            next_topic = re.search(next_pattern, paragraph[start + len(term):], re.I)
            end = start + len(term) + next_topic.start() if next_topic else len(paragraph)
            topic_text = paragraph[start:end]
            for title, number in re.findall(
                    r"（课件：([^，（）]+)，第\s*(\d+)\s*页(?:；[^）]*)?）", topic_text):
                page = pages.get((title, f"第 {int(number)} 页"))
                if page is not None and not any(alias.casefold() in page.casefold()
                                                for alias in aliases):
                    issues.append(f"{term} 段落引用的 {title} 第 {number} 页未提及该术语")
    for sentence in re.split(r"[。！？\n]+", body):
        if not sentence.strip():
            continue
        if re.search(r"(?:模)?运算.{0,12}(?:具备|具有|满足).{0,12}对称性.{0,8}传递性"
                     r".{0,12}(?:交换律|结合律)", sentence):
            issues.append("把同余关系的对称性、传递性误归为模运算的运算律")
        for word, stated in re.findall(
                r"去重得\s*[\"“]?([A-Za-z]{3,})[\"”]?\s*共\s*(\d+)\s*个字母", sentence):
            if len(set(word.casefold())) != int(stated):
                issues.append(
                    f"密钥短语去重计数矛盾：{word} 有 {len(set(word.casefold()))} 个不同字母，不是 {stated} 个")
        if ("SM4" in sentence and re.search(r"(?:算法.{0,8}保密|双重保密)", sentence)
                and re.search(r"商密|商用|商业|标准", sentence)
                and not ("军事" in sentence and
                         re.search(r"(?:商用.{0,12})?SM4.{0,12}(?:算法)?公开", sentence))):
            issues.append("把公开的商用 SM4 与军事密码的算法保密策略混为一谈")
        if re.search(r"SM4\s*[（(]?\s*(?:简称|缩写为|即|也叫)\s*商密", sentence, re.I):
            issues.append("把具体算法 SM4 误称为商用密码这一类别的简称")
        if re.search(r"只要.{0,15}密钥保密.{0,15}(?:系统|算法).{0,8}安全", sentence):
            issues.append("把密钥保密写成密码系统安全的充分条件")
        if re.search(r"(?:安全性.{0,8}(?:完全|仅仅|只).{0,12}密钥.{0,6}保密|"
                     r"密钥.{0,6}保密.{0,10}(?:完全|仅仅|只).{0,8}安全)", sentence):
            issues.append("把密钥保密写成密码系统安全的充分条件")
        if re.search(r"(?:完全|仅仅|只|仅).{0,5}依赖于密钥.{0,5}保密", sentence):
            issues.append("把密钥保密写成密码系统安全的充分条件")
        if re.search(r"(?:全世界|全球).{0,20}无法发现.{0,8}漏洞.{0,12}(?:证明|说明).{0,15}安全", sentence):
            issues.append("把尚未发现漏洞写成安全性的证明")
        if re.search(r"(?:全世界|全球).{0,20}(?:没有|未能|无法)发现.{0,8}漏洞"
                     r".{0,12}(?:证明|说明).{0,15}安全", sentence):
            issues.append("把尚未发现漏洞写成安全性的证明")
        if re.search(r"(?:全世界|全球).{0,12}(?:无人|没有人|未能|无法).{0,8}"
                     r"(?:攻破|破解).{0,12}(?:证明|说明|表明).{0,12}安全", sentence):
            issues.append("把尚未被攻破写成安全性的证明")
        if (re.search(r"(?:古典密码|凯撒密码|移位密码).{0,20}"
                      r"(?:往往|通常|一般|普遍|常常).{0,6}(?:将|把)?算法公开", sentence)
                and re.search(r"(?:算法.{0,8}(?:隐蔽|保密|秘密)|隐蔽性)", sentence)):
            issues.append("把依赖算法保密的古典密码误写成通常公开算法")
        if re.search(r"Playfair.{0,45}两个字母.{0,25}(?:代替|替换).{0,8}一个字母", sentence, re.I):
            issues.append("Playfair 把双字母对误写成单字母输出")
        if re.search(r"多对一映射.{0,20}(?:确保|保证).{0,8}(?:解密)?可逆", sentence):
            issues.append("把多对一映射误说成保证可逆")
        if re.search(r"多表代替.{0,20}(?:等同|相当于).{0,8}分组加密", sentence):
            issues.append("把多表代替误等同于分组加密")
        if re.search(r"一次一密.{0,80}每个密钥.{0,10}(?:仅|只).{0,8}一个比特", sentence):
            issues.append("把一次一密的整条密钥误写成单比特密钥")
        if re.search(r"[\"“]([A-Z]{2,})[\"”].{0,60}[\"“]([A-Z]{2,})[\"”]", sentence):
            for left, right in re.findall(
                    r"[\"“]([A-Z]{2,})[\"”].{0,60}[\"“]([A-Z]{2,})[\"”]", sentence):
                if len(left) != len(right) and re.search(r"替换|对应|映射|推断", sentence):
                    issues.append(f"单表代换示例字符数不等：{left} / {right}")
    playfair_page = pages.get(("ch02-古典密码.pdf", "第 147 页"), "")
    if ("相同对中的字母加分隔符" in playfair_page
            and re.search(r"Playfair.{0,180}同行.{0,35}同列.{0,35}交叉", body, re.I | re.S)
            and not re.search(r"(?:相同|重复).{0,12}字母.{0,18}(?:分隔|插入|填充|加\s*x)|"
                              r"(?:分隔|插入|填充).{0,8}(?:x|X).{0,20}(?:相同|重复)|"
                              r"balloon\s*(?:→|->)\s*ba\s*lx", body, re.I)):
        issues.append("Playfair 三种位置规则齐全，但漏掉同对相同字母插入分隔符的规则")
    inverse_page = pages.get(("ch02-古典密码.pdf", "第 52 页"), "")
    if "Q X1 X2 X3 Y1 Y2 Y3" in inverse_page and "-19" in inverse_page:
        for paragraph in re.split(r"\n\s*\n", body):
            if ("扩展欧几里得" not in paragraph
                    or not re.search(r"21.{0,16}模.{0,12}50", paragraph)
                    or not re.search(r"(?<!\d)31(?!\d)", paragraph)):
                continue
            # The real slide has the intermediate remainder chain
            # 8, 5, 3, 2, 1. An initial state plus the generic update rule
            # and final 31 cannot teach an absent student how -19 arose.
            shown = sum(bool(re.search(rf"(?<!\d){value}(?!\d)", paragraph))
                        for value in ("8", "5", "3", "2"))
            if shown < 3:
                issues.append("21 模 50 的扩展欧几里得例题缺少课件第 52 页的关键中间计算，无法照笔记复算")
    return list(dict.fromkeys(issues))


def strip_conflicting_paragraphs(
        note: str, sources: list[dict] | None = None,
) -> tuple[str, list[tuple[str, str]]]:
    """Remove only paragraphs that carry their own hard conflicts.

    Coverage first: one conflicting paragraph must not cost the whole
    chapter, so it leaves the body visibly and is returned with its first
    issue for the caller's pending zone. Note-level completeness checks
    span several paragraphs; the single-paragraph probe deliberately drops
    the two slide pages they depend on, so incomplete-but-correct
    paragraphs are never stripped here.
    """
    if "## 课堂内容" in note:
        head, marker, rest = note.partition("## 课堂内容")
        head += marker
        body, tail_marker, tail = rest.partition("## 作业与考试口头线索")
        tail = tail_marker + tail if tail_marker else ""
    else:
        head, body, tail = "", note, ""
    probe_sources = [
        row for row in (sources or [])
        if (str(row.get("title") or ""), str(row.get("locator") or "")) not in {
            ("ch02-古典密码.pdf", "第 147 页"),
            ("ch02-古典密码.pdf", "第 52 页"),
        }
    ]
    kept: list[str] = []
    removed: list[tuple[str, str]] = []
    for paragraph in re.split(r"\n\s*\n", body):
        issues = (note_factual_conflicts(paragraph, probe_sources)
                  if paragraph.strip() else [])
        if issues:
            removed.append((paragraph.strip(), issues[0]))
        else:
            kept.append(paragraph)
    if not removed:
        return note, []
    cleaned = "\n\n".join(kept).strip()
    parts = [part for part in (head.rstrip(), cleaned, tail.strip()) if part]
    return "\n\n".join(parts), removed


def audit_note(
    note: str,
    transcript: dict,
    *,
    sources: list[dict] | None = None,
    assignments: list[dict] | None = None,
    assignment_catalog_complete: bool = False,
    published_blocks: list[dict] | None = None,
    attendance_items: list[str] | None = None,
    required_claims: list[dict] | None = None,
    manual_review: dict | None = None,
) -> dict:
    """Return checks with pass/fail/review/unverified statuses, without network I/O."""
    checks: list[dict[str, str]] = []
    segments = transcript.get("segments") or []
    end = max((float(row.get("end", row.get("start", 0))) for row in segments), default=0.0)
    markers = [_stamp_seconds(*match) for match in re.findall(
        r"已整理至录像\s+(\d+):(\d\d)", note)]
    markers += [_stamp_seconds(*match) for match in re.findall(
        r"^##\s+\d+:\d\d[–-](\d+):(\d\d)\s*$", note, re.M)]
    if end <= 0 or not segments:
        checks.append(_check("整节时间覆盖", "unverified", "缺少带时间的转写段"))
    elif not markers:
        checks.append(_check("整节时间覆盖", "fail", "笔记没有可核对的结束标记"))
    else:
        gap = round(end - max(markers))
        checks.append(_check("整节时间覆盖", "pass" if gap <= 90 else "fail",
                             f"转写结束 {round(end)} 秒；笔记结束标记 {max(markers)} 秒；差 {gap} 秒"))

    has_classroom_section = "## 课堂内容" in note
    body = note.split("## 课堂内容", 1)[-1]
    body = body.split("## 作业与考试口头线索", 1)[0]
    body = body.split("*已整理至录像", 1)[0]
    if not has_classroom_section:
        checks.append(_check("内容密度", "fail", "缺少课堂内容正文"))
    elif end > 1800 and len(body.strip()) < end * 0.75:
        checks.append(_check("内容密度", "review", "长课笔记偏短；需人工核对是否遗漏主题"))
    else:
        checks.append(_check("内容密度", "pass" if body.strip() else "fail",
                             f"课堂正文 {len(body.strip())} 字符；此项不证明知识点完整"))

    if sources is None:
        checks.append(_check("课件页来源", "unverified", "未提供本讲明确关联的课件页清单"))
    else:
        readable = {(str(row.get("title", "")), str(row.get("locator", "")))
                    for row in sources if row.get("status") == "readable" and row.get("text")}
        cited: set[tuple[str, str]] = set()
        for line in note.splitlines():
            if "课件依据：" not in line and "课件：" not in line:
                continue
            for title, locator in re.findall(
                r"(?:课件依据：|课件：)\s*([^、，：*（）()]+?\.(?:pdf|pptx|docx))"
                r"\s*[,，]?\s*(第\s*\d+\s*[页张]|正文)",
                line, re.I):
                cited.add((title.strip(), re.sub(r"\s+", " ", locator.strip())))
        invalid = sorted(cited - readable)
        if invalid:
            checks.append(_check("课件页来源", "fail", f"引用了未证实可读的课件页：{invalid[:5]}"))
        elif readable and not cited:
            checks.append(_check("课件页来源", "review", "有可读课件但课堂正文没有页码引用"))
        else:
            checks.append(_check("课件页来源", "pass", f"核对 {len(cited)} 个课件页引用；可读页 {len(readable)} 个"))
        uncertain = [row for row in sources if row.get("status") in
                     {"partial", "unreadable", "truncated", "unavailable"}]
        checks.append(_check("课件读取缺口", "review" if uncertain else "pass",
                             f"{len(uncertain)} 个未完整读取的页面或文件；应查看原件" if uncertain
                             else "已提供的课件页均可读取"))

    published_text, published_links = _published_content(published_blocks)
    if assignments is None or published_blocks is None:
        checks.append(_check("正式作业原件与提交", "unverified", "需同时提供教学网正式作业和发布页区块"))
    elif not assignments:
        checks.append(_check("正式作业原件与提交", "pass" if assignment_catalog_complete else "unverified",
                             "完整同步的课程目录没有正式作业" if assignment_catalog_complete else
                             "提供的作业列表为空，但未证明教学网作业目录已完整同步"))
    else:
        missing: list[str] = []
        from .materials import safe_source_link

        published_urls = set(published_links.splitlines())
        for item in assignments:
            title = str(item.get("title") or "")
            if title and title not in published_text:
                missing.append(f"{title}: 标题")
            instructions = str(item.get("instructions") or "")
            if instructions and instructions not in published_text:
                missing.append(f"{title}: 老师原要求")
            raw_due = str(item.get("due_at") or "")
            if raw_due:
                try:
                    due = datetime.fromisoformat(raw_due.replace("Z", "+00:00"))
                    if due.tzinfo:
                        due = due.astimezone(timezone(timedelta(hours=8)))
                    if due.strftime("%Y-%m-%d %H:%M") not in published_text:
                        missing.append(f"{title}: 北京时间截止")
                except ValueError:
                    missing.append(f"{title}: 截止时间无法解析")
            for asset in item.get("attachments") or []:
                name = str(asset.get("filename") or "")
                path = str(asset.get("path") or "")
                if name and name not in published_text:
                    missing.append(f"{title}: 附件名 {name}")
                if path and not _attachment_link_present(published_links, path):
                    missing.append(f"{title}: 附件链接 {name}")
            for link in item.get("source_links") or []:
                if not isinstance(link, dict):
                    continue
                original = safe_source_link(link.get("label"), link.get("url"))
                if original and unquote(original.url) not in published_urls:
                    missing.append(f"{title}: 老师原页面链接 {original.label}")
            content_id = str(item.get("content_id") or "")
            if re.fullmatch(r"_\d+_\d+", content_id) and not _assignment_link_present(published_links, item):
                missing.append(f"{title}: 提交入口")
        status = "fail" if missing else "pass" if assignment_catalog_complete else "unverified"
        checks.append(_check("正式作业原件与提交", status,
                             "；".join(missing[:12]) if missing else
                             f"核对 {len(assignments)} 条正式作业的标题、原要求、截止、附件和提交入口；"
                             + ("目录已标为完整同步" if assignment_catalog_complete else "仍需确认目录已完整同步")))

    # A complete formal assignment index says nothing about handbook, notice,
    # or oral coursework. Keep a blanket negative claim out of student notes.
    unsupported_absence = []
    for sentence in re.split(r"[。！？；;\n]+", note):
        if (re.search(r"(?:未(?:布置|安排)|没有(?:布置|安排)?|无)(?:[^，,]{0,12})(?:作业|考试|课业|提交任务)", sentence)
                and not re.search(r"教学网|正式作业索引|正式作业目录|转写|未检出", sentence)):
            unsupported_absence.append(sentence.strip())
    checks.append(_check(
        "课业否定断言", "fail" if unsupported_absence else "pass",
        "不能从正式索引为空推断课程没有课业：" + "；".join(unsupported_absence[:3])
        if unsupported_absence else "没有把未查到的课业写成明确不存在",
    ))

    if attendance_items is None:
        checks.append(_check("必须到场事项", "unverified", "未提供独立核实的到场事项清单"))
    else:
        whole = note + "\n" + published_text
        missing = [item for item in attendance_items if item not in whole]
        if attendance_items and not re.search(r"必须到场|现场参与|不能代替本人参加|须到课", whole):
            missing.append("缺少到场性质的明确标记")
        checks.append(_check("必须到场事项", "fail" if missing else "pass",
                             "缺少：" + "；".join(missing) if missing else f"核对 {len(attendance_items)} 项到场要求"))

    conflicts: list[str] = []
    source_text = "\n".join(str(row.get("text") or "") for row in sources or []
                            if row.get("status") == "readable")
    for number in set(re.findall(r"(\d+(?:\.\d+)?)\s*(?:(?:pounds?|lbs?|kg)\b|磅|公斤)", source_text, re.I)):
        if re.search(rf"体重[^。；\n]{{0,35}}{re.escape(number)}\s*%", body):
            conflicts.append(f"课件把 {number} 记为重量，笔记同段写为百分比")
    if re.search(r"Middle Three Months[^\n]{0,100}(?:6|six)\s+times", source_text, re.I):
        if re.search(r"前三个月[^。；\n]{0,30}(?:六|6)\s*倍", body):
            conflicts.append("课件中间三个月的六倍增长被写成前三个月")
    if re.search(r"前三个月[^。；\n]{0,15}(?:即|也就是)\s*中间三个月", body):
        conflicts.append("笔记把前三个月和中间三个月当作同一时期")
    if re.search(r"(?:embryonic\s+stage|胚胎期)[^\n]{0,35}2\s*[–-]\s*8\s*weeks", source_text, re.I):
        if re.search(r"第?四周[^。；\n]{0,20}胚胎期的?起始", body):
            conflicts.append("课件定义胚胎期为第 2–8 周，笔记却称第 4 周起始")
    if re.search(r"45\s*%(?:\s+or\s+more)?(?:\s+of)?\s+(?:all\s+)?pregnanc|"
                 r"(?:全部妊娠|所有妊娠)[^\n]{0,35}45\s*%", source_text, re.I):
        if re.search(r"着床后[^。；\n]{0,35}45\s*%[^。；\n]{0,20}(?:流产|风险)", body):
            conflicts.append("课件的全部妊娠 45% 被笔记改写为着床后条件概率")
    checks.append(_check("明显时间线与数值冲突", "fail" if conflicts else
                         "pass" if sources is not None else "unverified",
                         "；".join(conflicts) if conflicts else
                         "未命中已知冲突规则；仍需人工核对其他数值、因果和术语"))
    factual = note_factual_conflicts(note, sources)
    checks.append(_check("已知事实与引用错误", "fail" if factual else "pass",
                         "；".join(factual[:8]) if factual else
                         "未命中已知硬错误；其他事实仍需人工逐句核对"))

    time_headings = re.findall(r"^#{2,4}\s*\d+:\d\d\s*[–-]\s*\d+:\d\d", body, re.M)
    boilerplate = len(re.findall(r"^#{2,4}\s*(?:待核问题|要点列表|关键概念|总结)\s*$", body, re.M))
    raw_asr = "以下为原始转录" in body
    # A real G02 stronger-model trial produced "tobacco，alcohol和不行":
    # exact ASR provenance passed the ledger, but the conjunction is left
    # dangling with nothing after it, so the sentence reads as unfinished
    # transcription. The defect is the truncated clause, not the mixed
    # scripts: bilingual terminology is normal in these courses and must
    # stay usable, so only this narrow shape is flagged.
    dangling_clause = bool(re.search(
        r"(?i)[a-z]{3,}\s*[,，、]\s*[a-z]{3,}\s*(?:和|及|与)\s*"
        r"(?:不行|不能|可以|是|会|没有)(?:[，。！？\s]|$)", body,
    ))
    spoken_restart = bool(re.search(r"[。！？]\s*(?:嗯|呃)[，,\s]*(?:就是|这个|那个)", body))
    unnatural = bool(time_headings or boilerplate >= 3 or raw_asr
                     or dangling_clause or spoken_restart)
    checks.append(_check("自然排版", "review" if unnatural else "pass",
                         f"时间段标题 {len(time_headings)} 个；模板标题 {boilerplate} 个；"
                         f"原始转录回退 {raw_asr}；未说完的残句 {dangling_clause}；"
                         f"口语重启 {spoken_restart}"))
    if required_claims is None:
        checks.append(_check("关键事实保留", "unverified",
                             "未提供由转写和课件共同核实的本讲关键事实清单；过滤错误句子可能同时删除正确知识点"))
    else:
        missing_claims = []
        for claim in required_claims:
            terms = [str(term) for term in claim.get("must_contain", []) if str(term)]
            if not terms or not all(term in body for term in terms):
                missing_claims.append(str(claim.get("label") or "未命名事实"))
        checks.append(_check("关键事实保留", "fail" if missing_claims else "pass",
                             "缺少：" + "；".join(missing_claims) if missing_claims else
                             f"核对 {len(required_claims)} 个关键事实；同义改写仍需人工判断"))
    fingerprint = review_fingerprint(
        note, transcript, sources=sources, assignments=assignments,
        assignment_catalog_complete=assignment_catalog_complete,
        published_blocks=published_blocks, attendance_items=attendance_items,
        required_claims=required_claims,
    )
    review_items = ("topic_coverage", "claim_grounding", "oral_requirements", "source_alignment")
    signed = bool(manual_review and manual_review.get("reviewer") and manual_review.get("reviewed_at")
                  and manual_review.get("review_fingerprint") == fingerprint
                  and all(manual_review.get(key) is True for key in review_items))
    checks.append(_check("人工逐项审校", "pass" if signed else "unverified",
                         "已记录审校人、日期、四项检查和本次证据指纹" if signed else
                         "需人工核对知识点覆盖、事实与数值、口头要求、逐条课件页对应关系；"
                         "审校记录还必须匹配本次笔记及证据指纹"))
    return {"verdict": "fail" if any(row["status"] == "fail" for row in checks)
            else "incomplete" if any(row["status"] in {"review", "unverified"} for row in checks)
            else "pass", "checks": checks, "review_fingerprint": fingerprint,
            "meaning": "pass 仅表示给定证据和人工审校记录通过；不保证笔记能替代实际到场或覆盖未知通知"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline lecture-note acceptance checks; no network or writes")
    parser.add_argument("--note", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--sources", type=Path)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--assignment-catalog-complete", action="store_true",
                        help="Only set after verifying the course assignment catalog fully synced")
    parser.add_argument("--published-blocks", type=Path)
    parser.add_argument("--attendance-items", type=Path)
    parser.add_argument("--required-claims", type=Path)
    parser.add_argument("--manual-review", type=Path)
    args = parser.parse_args()

    def read_json(path: Path | None):
        return json.loads(path.read_text("utf-8")) if path else None

    report = audit_note(args.note.read_text("utf-8"), read_json(args.transcript),
                        sources=read_json(args.sources), assignments=read_json(args.assignments),
                        assignment_catalog_complete=args.assignment_catalog_complete,
                        published_blocks=read_json(args.published_blocks),
                        attendance_items=read_json(args.attendance_items),
                        required_claims=read_json(args.required_claims),
                        manual_review=read_json(args.manual_review))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["verdict"] == "pass" else 1)


if __name__ == "__main__":
    main()
