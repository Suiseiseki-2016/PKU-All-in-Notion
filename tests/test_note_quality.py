"""Synthetic acceptance cases; no real course content or account data."""

from pku_sync.note_quality import audit_note


def _block(text: str, url: str = "") -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": [{
        "type": "text", "text": {"content": text, "link": {"url": url} if url else None},
    }]}}


def _note(body: str, end: str = "20:00") -> str:
    return ("# 示例课程\n\n## 本讲来源\n\n## 课堂内容\n\n" + body
            + "\n\n*已整理至录像 " + end + "。*\n")


def _transcript():
    return {"segments": [{"start": 0, "end": 1200, "text": "老师讲了基本概念"}]}


def test_complete_timeline_does_not_claim_semantic_completeness():
    result = audit_note(_note("概念。"), _transcript())
    assert result["checks"][0]["status"] == "pass"
    assert result["verdict"] == "incomplete"
    assert any(row["status"] == "unverified" for row in result["checks"])


def test_natural_layout_audits_classroom_body_without_raw_oral_appendix():
    note = _note("自然讲解。\n\n## 作业与考试口头线索（自动转写原文，待核对）\n"
                 "- [18:20] 以下为原始转录：嗯，就是那个作业。")
    report = audit_note(note, _transcript())
    assert next(row["status"] for row in report["checks"] if row["name"] == "自然排版") == "pass"


def test_note_ending_early_fails_even_when_it_has_sources():
    result = audit_note(_note("概念。", "05:00"), _transcript(), sources=[])
    assert result["verdict"] == "fail"
    assert result["checks"][0]["status"] == "fail"


def test_cited_page_must_exist_and_be_readable():
    source = {"title": "第2周.pdf", "locator": "第 4 页", "text": "术语定义", "status": "readable"}
    note = _note("术语说明。\n\n*课件依据：第2周.pdf 第 5 页（用于核对概念）。*")
    result = audit_note(note, _transcript(), sources=[source])
    assert next(row for row in result["checks"] if row["name"] == "课件页来源")["status"] == "fail"


def test_canonical_generated_citation_is_counted_by_acceptance_audit():
    source = {"title": "古典密码.pdf", "locator": "第 4 页", "text": "术语定义", "status": "readable"}
    good = audit_note(_note("术语说明（课件：古典密码.pdf，第 4 页）。"), _transcript(), sources=[source])
    bad = audit_note(_note("术语说明（课件：古典密码.pdf，第 5 页）。"), _transcript(), sources=[source])
    status = lambda report: next(row["status"] for row in report["checks"] if row["name"] == "课件页来源")
    assert status(good) == "pass"
    assert status(bad) == "fail"


def test_unreadable_source_marks_review_even_when_other_page_is_cited():
    sources = [
        {"title": "第2周.pdf", "locator": "第 4 页", "text": "术语定义", "status": "readable"},
        {"title": "第2周.pdf", "locator": "第 5 页", "text": "", "status": "partial"},
    ]
    note = _note("术语说明。\n\n*课件依据：第2周.pdf 第 4 页（用于核对概念）。*")
    result = audit_note(note, _transcript(), sources=sources)
    assert next(row for row in result["checks"] if row["name"] == "课件页来源")["status"] == "pass"
    assert next(row for row in result["checks"] if row["name"] == "课件读取缺口")["status"] == "review"


def test_formal_assignment_requires_exact_teacher_text_due_attachment_and_submission():
    assignment = [{"course_id": "_99_1", "title": "第一次作业", "instructions": "完成原题并上传 PDF",
                   "due_at": "2026-10-01T15:30:00Z", "content_id": "_12_1",
                   "attachments": [{"filename": "原题.pdf", "path": "/bbcswebdav/original.pdf"}]}]
    blocks = [
        _block("第一次作业"), _block("完成原题并上传 PDF"),
        _block("截止：2026-10-01 23:30 北京时间"),
        _block("原题.pdf", "https://course.pku.edu.cn/bbcswebdav/original.pdf"),
        _block("提交入口", "https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_99_1&content_id=_12_1"),
    ]
    result = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                        published_blocks=blocks, assignment_catalog_complete=True)
    assert next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["status"] == "pass"
    blocks.pop()
    result = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                        published_blocks=blocks, assignment_catalog_complete=True)
    assert "提交入口" in next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["detail"]


def test_formal_assignment_keeps_teacher_starter_link_in_published_card():
    assignment = [{
        "course_id": "_99_1", "title": "Lab1发布", "content_id": "_12_1",
        "instructions": "按模板完成 Lab1", "source_links": [{
            "label": "GitHub 模板",
            "url": "https://github.com/N2Sys-EDU/2026-lab1-myFTP-Template",
        }],
    }]
    blocks = [_block("Lab1发布"), _block("按模板完成 Lab1"),
              _block("提交入口", "https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_99_1&content_id=_12_1")]
    report = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                        published_blocks=blocks, assignment_catalog_complete=True)
    check = next(row for row in report["checks"] if row["name"] == "正式作业原件与提交")
    assert check["status"] == "fail"
    assert "老师原页面链接 GitHub 模板" in check["detail"]
    blocks.append(_block("GitHub 模板", "https://github.com/N2Sys-EDU/2026-lab1-myFTP-Template"))
    report = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                        published_blocks=blocks, assignment_catalog_complete=True)
    assert next(row for row in report["checks"] if row["name"] == "正式作业原件与提交")["status"] == "pass"


def test_formal_assignment_rejects_links_for_other_course_or_lookalike_attachment():
    assignment = [{"course_id": "_99_1", "title": "第一次作业", "instructions": "按原题完成",
                   "content_id": "_12_1", "attachments": [
                       {"filename": "原题.pdf", "path": "/bbcswebdav/original.pdf"}]}]
    blocks = [
        _block("第一次作业"), _block("按原题完成"),
        _block("原题.pdf", "https://course.pku.edu.cn/bbcswebdav/original.pdf.bak"),
        _block("提交入口", "https://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_88_1&content_id=_12_1"),
    ]
    report = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                        published_blocks=blocks, assignment_catalog_complete=True)
    check = next(row for row in report["checks"] if row["name"] == "正式作业原件与提交")
    assert check["status"] == "fail"
    assert "附件链接 原题.pdf" in check["detail"]
    assert "提交入口" in check["detail"]
    blocks[2] = _block("原题.pdf", "http://course.pku.edu.cn/bbcswebdav/original.pdf")
    blocks[3] = _block("提交入口", "http://course.pku.edu.cn/webapps/assignment/uploadAssignment?course_id=_99_1&content_id=_12_1")
    insecure = audit_note(_note("课堂内容。"), _transcript(), assignments=assignment,
                          published_blocks=blocks, assignment_catalog_complete=True)
    check = next(row for row in insecure["checks"] if row["name"] == "正式作业原件与提交")
    assert check["status"] == "fail"
    assert "附件链接 原题.pdf" in check["detail"] and "提交入口" in check["detail"]


def test_empty_assignments_need_complete_catalog_evidence():
    result = audit_note(_note("课堂内容。"), _transcript(), assignments=[], published_blocks=[])
    assert next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["status"] == "unverified"
    result = audit_note(_note("课堂内容。"), _transcript(), assignments=[], published_blocks=[],
                        assignment_catalog_complete=True)
    assert next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["status"] == "pass"


def test_empty_formal_index_does_not_prove_no_coursework():
    result = audit_note(_note("本次课程未布置具体作业或考试任务。"), _transcript(),
                        assignments=[], published_blocks=[], assignment_catalog_complete=True)
    assert next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["status"] == "pass"
    assert next(row for row in result["checks"] if row["name"] == "课业否定断言")["status"] == "fail"
    assert result["verdict"] == "fail"


def test_scope_limited_formal_index_status_is_allowed():
    result = audit_note(_note("教学网正式作业索引目前为 0 项；课程手册和老师通知仍需核对。"),
                        _transcript(), assignments=[], published_blocks=[],
                        assignment_catalog_complete=True)
    assert next(row for row in result["checks"] if row["name"] == "课业否定断言")["status"] == "pass"


def test_null_notion_text_does_not_crash_assignment_check():
    blocks = [{"type": "paragraph", "paragraph": {"rich_text": [
        {"type": "mention", "text": None, "plain_text": "正式作业目录"},
    ]}}]
    result = audit_note(_note("课堂内容。"), _transcript(), assignments=[],
                        published_blocks=blocks, assignment_catalog_complete=True)
    assert next(row for row in result["checks"] if row["name"] == "正式作业原件与提交")["status"] == "pass"


def test_attendance_must_be_named_and_marked_as_in_person():
    note = _note("课堂展示需要组员参加。")
    result = audit_note(note, _transcript(), attendance_items=["课堂展示"])
    assert next(row for row in result["checks"] if row["name"] == "必须到场事项")["status"] == "fail"
    result = audit_note(_note("课堂展示必须到场，笔记不能代替本人参加。"),
                        _transcript(), attendance_items=["课堂展示"])
    assert next(row for row in result["checks"] if row["name"] == "必须到场事项")["status"] == "pass"


def test_known_unit_and_timeline_conflicts_are_release_failures():
    sources = [
        {"status": "readable", "title": "课件.pdf", "locator": "第 5 页",
         "text": "Middle Three Months: Brain grows 6 times in size."},
        {"status": "readable", "title": "课件.pdf", "locator": "第 6 页",
         "text": "Weight increases 4.5 pounds in the last 10 weeks."},
    ]
    note = _note("前三个月大脑增长六倍。最后十周体重增加 4.5%。")
    result = audit_note(note, _transcript(), sources=sources)
    conflict = next(row for row in result["checks"] if row["name"] == "明显时间线与数值冲突")
    assert conflict["status"] == "fail"
    assert "六倍" in conflict["detail"] and "4.5" in conflict["detail"]


def test_rsa_citation_must_point_to_page_that_actually_names_rsa():
    from pku_sync.note_quality import note_factual_conflicts

    pages = [
        {"status": "readable", "title": "ch02-古典密码.pdf", "locator": "第 30 页",
         "text": "1977年Rivest，Shamir & Adleman提出了RSA公钥算法"},
        {"status": "readable", "title": "ch02-古典密码.pdf", "locator": "第 36 页",
         "text": "代替密码用一个字符替换另一个字符。"},
    ]
    wrong = _note("三位作者提出 RSA 公钥算法。（课件：ch02-古典密码.pdf，第 36 页）")
    right = _note("三位作者提出 RSA 公钥算法。（课件：ch02-古典密码.pdf，第 30 页）")
    assert any("RSA 论断错引" in issue for issue in note_factual_conflicts(wrong, pages))
    assert not any("RSA 论断错引" in issue for issue in note_factual_conflicts(right, pages))


def test_stage_start_and_conditional_probability_conflicts():
    sources = [{"status": "readable", "title": "课件.pdf", "locator": "第 2 页",
                "text": "The embryonic stage (2–8 weeks). Roughly 45% or more pregnancies miscarry."}]
    note = _note("第四周是胚胎期的起始阶段。着床后仍有约 45% 的风险发生流产。")
    result = audit_note(note, _transcript(), sources=sources)
    detail = next(row for row in result["checks"] if row["name"] == "明显时间线与数值冲突")["detail"]
    assert "第 4 周" in detail and "着床后" in detail


def test_gold_claim_flags_correct_fact_lost_while_filtering_bad_sentence():
    note = _note("这节课讨论了胎儿生长。")
    claims = [{"label": "中间三个月脑发育", "must_contain": ["中间三个月", "六倍"]}]
    result = audit_note(note, _transcript(), required_claims=claims)
    check = next(row for row in result["checks"] if row["name"] == "关键事实保留")
    assert check["status"] == "fail" and "中间三个月脑发育" in check["detail"]


def test_manual_review_needs_explicit_four_part_signoff():
    base = dict(sources=[], assignments=[], published_blocks=[], attendance_items=[],
                required_claims=[], assignment_catalog_complete=True)
    result = audit_note(_note("完整课堂讲义。"), _transcript(), **base)
    assert result["verdict"] == "incomplete"
    signoff = {"reviewer": "测试审校人", "reviewed_at": "2026-09-27",
               "topic_coverage": True, "claim_grounding": True,
               "oral_requirements": True, "source_alignment": True,
               "review_fingerprint": result["review_fingerprint"]}
    result = audit_note(_note("完整课堂讲义。"), _transcript(), manual_review=signoff, **base)
    assert result["verdict"] == "pass"
    changed = audit_note(_note("课堂讲义另加一个事实。"), _transcript(),
                         manual_review=signoff, **base)
    assert next(row for row in changed["checks"] if row["name"] == "人工逐项审校")["status"] == "unverified"
    changed_source = audit_note(_note("完整课堂讲义。"), _transcript(),
                                manual_review=signoff, **{**base, "sources": [
                                    {"title": "修订课件.pdf", "locator": "第 1 页",
                                     "text": "新的课程事实", "status": "readable"}]})
    assert next(row for row in changed_source["checks"]
                if row["name"] == "人工逐项审校")["status"] == "unverified"


def test_time_index_and_raw_transcript_fallback_need_review():
    note = _note("## 00:00–08:00\n第一段。\n\n## 08:00–16:00\n第二段。\n以下为原始转录：")
    result = audit_note(note, _transcript(), sources=[])
    assert next(row for row in result["checks"] if row["name"] == "自然排版")["status"] == "review"


def test_real_g02_dangling_clause_is_not_a_natural_note():
    note = _note("抽烟喝酒不可以哈。 嗯，就是tobacco，alcohol和不行。")
    base = dict(sources=[], assignments=[], published_blocks=[], attendance_items=[],
                required_claims=[], assignment_catalog_complete=True)
    result = audit_note(note, _transcript(), **base)
    check = next(row for row in result["checks"] if row["name"] == "自然排版")
    assert check["status"] == "review"
    assert "未说完的残句 True" in check["detail"]
    signoff = {"reviewer": "测试审校人", "reviewed_at": "2026-09-28",
               "topic_coverage": True, "claim_grounding": True,
               "oral_requirements": True, "source_alignment": True,
               "review_fingerprint": result["review_fingerprint"]}
    signed = audit_note(note, _transcript(), manual_review=signoff, **base)
    assert next(row for row in signed["checks"] if row["name"] == "人工逐项审校")["status"] == "pass"
    assert signed["verdict"] == "incomplete"


def test_clean_bilingual_terminology_does_not_trigger_broken_sentence_rule():
    note = _note("课件将 tobacco 与 alcohol 列为需区分的暴露类别。")
    result = audit_note(note, _transcript(), sources=[])
    assert next(row for row in result["checks"] if row["name"] == "自然排版")["status"] == "pass"


def test_glossed_english_terms_read_as_a_natural_note():
    """These courses teach English terms; glossing them is good note-taking."""
    note = _note("明文（plaintext）经加密变成密文（ciphertext），"
                 "Playfair 密码把明文两两分组后再查表替换。")
    result = audit_note(note, _transcript(), sources=[])
    check = next(row for row in result["checks"] if row["name"] == "自然排版")
    assert check["status"] == "pass"
    assert "未说完的残句 False" in check["detail"]


def test_real_cryptography_hard_errors_fail_even_when_cited_pages_exist():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"status": "readable", "title": "古典密码.pdf", "locator": "第 15 页",
         "text": "分组密码 DES、AES、SM4"},
        {"status": "readable", "title": "古典密码.pdf", "locator": "第 63 页",
         "text": "密钥短语密码 university"},
        {"status": "readable", "title": "古典密码.pdf", "locator": "第 18 页",
         "text": "密码分析 唯密文攻击 已知明文攻击"},
    ]
    note = _note(
        "SM4 是商密标准，既要求算法保密，也要求密钥保密（课件：古典密码.pdf，第 15 页）。"
        "只要密钥保密，系统依然安全。"
        "单表代换中将“IQI”对应为“THESE”。"
        "Playfair 密码用 5×5 方阵（课件：古典密码.pdf，第 63 页）。"
        "希尔密码使用矩阵（课件：古典密码.pdf，第 18 页）。"
    )
    issues = note_factual_conflicts(note, sources)
    assert len(issues) == 5
    assert any("SM4" in issue for issue in issues)
    assert any("Playfair" in issue for issue in issues)
    assert any("Hill" in issue for issue in issues)
    report = audit_note(note, _transcript(), sources=sources)
    assert next(row for row in report["checks"] if row["name"] == "已知事实与引用错误")["status"] == "fail"


def test_matching_cryptography_pages_and_equal_length_mapping_pass_known_check():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [{"status": "readable", "title": "古典密码.pdf", "locator": "第 1 页",
                "text": "Playfair 密码。Hill 希尔密码。"}]
    note = _note("商用 SM4 的算法公开。单表代换将“IQI”对应为“ETE”。"
                 "Playfair 和 Hill 有不同的加密规则（课件：古典密码.pdf，第 1 页）。")
    assert note_factual_conflicts(note, sources) == []


def test_impossible_monoalphabetic_mapping_is_removed_from_note():
    from pku_sync.media import _filter_unsupported_claims

    chapter = ("本章讨论单表代换。密文“IQI”可对应明文“THESE”，"
               "据此推断密钥。频率分析仍可用于破译。")
    cleaned = _filter_unsupported_claims(chapter, [], "单表代换的频率分析")
    assert "IQI" not in cleaned and "THESE" not in cleaned
    assert "频率分析仍可用于破译" in cleaned


def test_false_playfair_and_otp_claims_and_adjacent_wrong_pages_fail():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"title": "讲义.pdf", "locator": "第 18 页", "status": "readable",
         "text": "密码分析：唯密文攻击"},
        {"title": "讲义.pdf", "locator": "第 68 页", "status": "readable",
         "text": "英文中字母频率"},
    ]
    note = _note(
        "### Playfair 密码\n\n"
        "Playfair 每次将两个字母组合代替为一个字母。"
        "这种机制可抵抗频率分析（课件：讲义.pdf，第 68 页）。\n\n"
        "### 希尔密码\n\n希尔密码使用矩阵。"
        "它易受已知明文攻击（课件：讲义.pdf，第 18 页）。\n\n"
        "一次一密每个密钥仅加密一个比特。"
        "多对一映射用于确保解密可逆性。多表代替相当于分组加密。"
        "只要全世界的人都没有发现漏洞，就说明算法非常安全。"
    )
    issues = note_factual_conflicts(note, sources)
    assert any("Playfair 把双字母" in issue for issue in issues)
    assert any("Hill 段落引用" in issue for issue in issues)
    assert any("一次一密" in issue for issue in issues)
    assert any("多对一" in issue for issue in issues)
    assert any("多表代替" in issue for issue in issues)
    assert any("漏洞" in issue for issue in issues)


def test_keyphrase_letter_count_is_checked_even_if_slide_repeats_error():
    from pku_sync.media import _filter_unsupported_claims
    from pku_sync.note_quality import note_factual_conflicts

    bad = "单表代换以 university 去重得 universty 共 10 个字母。"
    assert any("9 个不同字母" in issue
               for issue in note_factual_conflicts(_note(bad)))
    assert _filter_unsupported_claims(bad, [], "") == ""


def test_kasiski_scope_and_ic_evidence_are_not_overclaimed():
    from pku_sync.note_quality import note_factual_conflicts

    bad = _note(
        "### 周期破译\n\n"
        "卡契斯基测试对长字符串（通常 4 字母以上）的重复距离求因数。\n\n"
        "重合指数 IC 各列平均接近 0.0687，因此证实周期猜测。"
    )
    issues = note_factual_conflicts(bad)
    assert any("重复串长度" in issue for issue in issues)
    assert any("支持误写成证明" in issue for issue in issues)

    good = _note(
        "卡契斯基分析一般考虑 4 个字母以上的密钥候选长度。\n\n"
        "各列 IC 平均接近 0.0687，支持周期为 6 的猜测，还需结合其他线索。"
    )
    assert not note_factual_conflicts(good)


def test_late_slide_math_errors_are_blocked():
    from pku_sync.note_quality import note_factual_conflicts

    bad = _note(
        "卡契斯基测试提取重复出现的字母串（通常要求长度在四字母以上）。\n\n"
        "模运算遵循分配律性质：先取模再相加。\n\n"
        "Hill 密码已知明文攻击只要有 m 个明密文对，即可还原出加密矩阵 K。\n\n"
        "系统的安全性仅依赖于密钥的保密。"
    )
    issues = note_factual_conflicts(bad)
    assert any("重复串长度" in issue for issue in issues)
    assert any("分配律" in issue for issue in issues)
    assert any("Hill 已知明文" in issue for issue in issues)
    assert any("充分条件" in issue for issue in issues)
    opposite = _note("* 卡契斯基测试法中的‘四字母以上’是重复字符串长度限制，而非密钥长度。")
    assert any("四字母条件" in issue for issue in note_factual_conflicts(opposite))
    short = _note("卡契斯基通过计算长串（通常考虑四字母以上）重复出现的距离猜测密钥长度。")
    assert any("长串的限制" in issue for issue in note_factual_conflicts(short))

    good_hill = _note(
        "Hill 密码有 m 个明密文对，且相应明文矩阵可逆时，"
        "才可据 K=YX^-1 求出密钥矩阵。"
    )
    assert not any("Hill 已知明文" in issue for issue in note_factual_conflicts(good_hill))


def test_modular_power_example_requires_the_missing_multiplication():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [{"title": "ch02-古典密码.pdf", "locator": "第 43 页",
                "status": "readable", "text": "11^7 mod 13 = 11 × 11^4 × 11^2 mod 13"}]
    incomplete = _note(
        "例如计算 $11^7 \\pmod Q$ 时，通过平方后取模的方法，"
        "先计算 $11^2$ 取模，再将结果平方并再次取模，"
        "可以确保中间结果保持在有限范围内。"
    )
    issues = note_factual_conflicts(incomplete, sources)
    assert any("7=4+2+1" in issue for issue in issues)
    report = audit_note(incomplete, _transcript(), sources=sources)
    assert next(row for row in report["checks"] if row["name"] == "已知事实与引用错误")["status"] == "fail"

    implicit_square = _note(
        "例如计算 $11^7 \\pmod Q$ 时，采用“平方后取模”策略"
        "（即先平方取模，再将结果平方并再次取模），"
        "可确保中间结果始终处于有限范围内。"
    )
    assert any("7=4+2+1" in issue
               for issue in note_factual_conflicts(implicit_square, sources))

    complete = _note(
        "计算 $11^7 \\pmod{13}$ 时，先求 $11^2$，再平方得 $11^4$，"
        "最后计算 $11 \\times 11^4 \\times 11^2 \\pmod{13}$。"
    )
    assert not any("11^7 的平方取模示例" in issue
                   for issue in note_factual_conflicts(complete, sources))


def test_inverse_example_requires_reproducible_intermediate_calculation():
    from pku_sync.note_quality import note_factual_conflicts

    source = [{"title": "ch02-古典密码.pdf", "locator": "第 52 页",
               "status": "readable",
               "text": "Q X1 X2 X3 Y1 Y2 Y3\n2 0 1 21 1 -2 8\n2 1 -2 8 -2 5 5\n"
                       "1 -2 5 5 3 -7 3\n1 3 -7 3 -5 12 2\n1 -5 12 2 8 -19 1"}]
    incomplete = _note(
        "扩展欧几里得算法：以计算 $21$ 模 $50$ 的逆元为例，先初始化三元组，"
        "再反复计算商和更新三元组，得到 $-19$，换算为 $31$。"
    )
    assert any("无法照笔记复算" in issue
               for issue in note_factual_conflicts(incomplete, source))

    complete = _note(
        "扩展欧几里得算法计算 $21$ 模 $50$ 的逆元：余数依次为"
        "$50,21,8,5,3,2,1$，对应的系数最后得到 $-19$，"
        "取模换算得 $31$，并可验证 $21\\times31\\equiv1\\pmod{50}$。"
    )
    assert not any("无法照笔记复算" in issue
                   for issue in note_factual_conflicts(complete, source))


def test_congruence_relation_properties_are_not_operation_laws():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"title": "ch02-古典密码.pdf", "locator": "第 42 页", "status": "readable",
         "text": "同余关系具有对称性与传递性；模运算满足交换律与结合律。"},
        {"title": "ch02-古典密码.pdf", "locator": "第 161 页", "status": "readable",
         "text": "模运算性质总结"},
    ]
    wrong = _note("该运算具备对称性、传递性、交换律及结合律。")
    assert any("同余关系" in issue for issue in note_factual_conflicts(wrong, sources))
    correct = _note("同余关系具有对称性和传递性；模加法与模乘法满足交换律和结合律。")
    assert not any("同余关系" in issue for issue in note_factual_conflicts(correct, sources))


def test_slide_history_does_not_merge_kahn_with_public_key_inventors():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"title": "ch02-古典密码.pdf", "locator": "第 29 页", "status": "readable",
         "text": "1967年David Kahn的The Codebreakers"},
        {"title": "ch02-古典密码.pdf", "locator": "第 30 页", "status": "readable",
         "text": "1976年Diffie & Hellman提出不对称密钥密码。1977年Rivest, Shamir & Adleman提出RSA。"},
    ]
    bad = _note("香农发表论文，大卫·卡恩于 1975 年出版《破译者》。"
                "他们设想加密和解密使用不同的密钥，次年这三位作者提出 RSA。")
    issues = note_factual_conflicts(bad, sources)
    assert any("1967" in issue for issue in issues)
    assert any("同一主语" in issue for issue in issues)
    vague = _note("IBM 于 1971 至 1973 年发布技术报告。次年三位作者设计 RSA 公钥算法。")
    assert any("次年三位作者" in issue for issue in note_factual_conflicts(vague, sources))


def test_adjacent_crypto_classification_items_do_not_conflate_key_types():
    from pku_sync.note_quality import note_factual_conflicts

    correct = _note(
        "按密钥特点分为两类：\n"
        "* **对称密码**：又称单密钥算法，加密密钥和解密密钥相同。\n"
        "* **非对称密码**：加密密钥和解密密钥不同，其中加密密钥可以公开（公钥），"
        "解密密钥保密（私钥）。"
    )
    assert not any("公钥私钥" in issue for issue in note_factual_conflicts(correct))

    correct_prose = _note(
        "对称密码又称单密钥算法，加密和解密使用相同密钥。"
        "非对称密码的加密密钥和解密密钥不同，从一个难以推导出另一个。"
        "其中加密密钥可以公开，称为公钥；解密密钥保密，称为私钥。"
    )
    assert not any("公钥私钥" in issue for issue in note_factual_conflicts(correct_prose))

    wrong = _note(
        "* **对称密码**：又称单密钥算法，密钥相同，其中加密密钥可以公开（公钥），"
        "解密密钥保密（私钥）。"
    )
    assert any("公钥私钥" in issue for issue in note_factual_conflicts(wrong))


def test_dropping_numeric_intro_does_not_drop_section_heading():
    from pku_sync.media import _filter_unsupported_claims
    from pku_sync.note_quality import note_factual_conflicts

    draft = (
        "**对称密码算法**\n"
        "对称密码使用相同的密钥，又称单密钥算法。\n\n"
        "**非对称密码算法**\n"
        "非对称密码算法出现于 1977 年。"
        "其中加密密钥可以公开，称为公钥；解密密钥保密，称为私钥。"
    )
    checked = _filter_unsupported_claims(draft, [], "")
    assert "1977" not in checked
    assert "**非对称密码算法**" in checked
    assert checked.index("**非对称密码算法**") < checked.index("公钥")
    assert not any("公钥私钥" in issue for issue in note_factual_conflicts(_note(checked)))


def test_undiscovered_vulnerabilities_are_not_proof_of_security():
    from pku_sync.media import _filter_unsupported_claims

    claim = "若全世界都无法发现漏洞，则说明该算法具备极高的安全性。"
    assert _filter_unsupported_claims(claim, [], "讨论了算法公开和审查。") == ""
    joined = "算法公开便于公众研究漏洞；若全世界的人都没有发现漏洞，则说明该算法很安全。"
    assert _filter_unsupported_claims(joined, [], "讨论了算法公开和审查。") == "算法公开便于公众研究漏洞。"
    guarantee = "只要密钥保密，系统依然安全。"
    assert _filter_unsupported_claims(guarantee, [], "讨论了密钥保密。") == ""


def test_kerckhoffs_principle_does_not_treat_key_secrecy_as_sufficient():
    from pku_sync.media import _filter_unsupported_claims
    from pku_sync.note_quality import note_factual_conflicts

    source = {"text": "密码算法应建立在算法公开不影响明文和密钥的安全之上。"}
    draft = ("现代密码遵循柯克霍夫原则，而安全性仅依赖于密钥的保密。"
             "通过必要的密钥保密来保证整体安全性。")
    fixed = _filter_unsupported_claims(draft, [source], "讨论柯克霍夫原则。")
    assert "仅依赖" not in fixed and "保证整体安全" not in fixed
    assert "算法公开本身不应降低系统安全性" in fixed
    assert not any("充分条件" in issue for issue in note_factual_conflicts(_note(fixed)))
    variant = "柯克霍夫原则说系统的安全性不应依赖于算法的保密，而应完全依赖于密钥的保密。"
    corrected = _filter_unsupported_claims(variant, [source], "讨论柯克霍夫原则。")
    assert "完全依赖" not in corrected
    assert "密钥仍须保密" in corrected


def test_one_time_pad_bit_per_key_misstatement_is_removed_without_dropping_context():
    from pku_sync.media import _filter_unsupported_claims

    claim = ("一次一密的原理是每个密钥仅加密一个比特且密钥不重复使用。"
             "密钥需要与明文等长。")
    fixed = _filter_unsupported_claims(claim, [], "介绍了一次一密与等长密钥。")
    assert "每条密钥只使用一次" in fixed
    assert "一个比特" not in fixed
    assert "密钥需要与明文等长" in fixed


def test_first_chapter_proof_sm4_and_classical_disclosure_errors_fail():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"title": "ch02-古典密码.pdf", "locator": "第 15 页", "status": "readable",
         "text": "分组密码：DES、IDEA、RC6、Rijndael（AES）、SM4"},
        {"title": "ch02-古典密码.pdf", "locator": "第 17 页", "status": "readable",
         "text": "Kerckhoff原则：算法公开不影响明文和密钥的安全；古典和现代密码的分界线"},
    ]
    bad = _note(
        "若全球无人能攻破，则证明其安全性极高。"
        "中国商用密码标准为 SM4（简称商密）。"
        "古典密码（如移位密码）往往将算法公开，其安全性仅靠算法本身的隐蔽性。"
    )
    issues = note_factual_conflicts(bad, sources)
    assert any("尚未被攻破" in issue for issue in issues)
    assert any("具体算法 SM4" in issue for issue in issues)
    assert any("古典密码误写成通常公开算法" in issue for issue in issues)
    report = audit_note(bad, _transcript(), sources=sources)
    assert next(row for row in report["checks"] if row["name"] == "已知事实与引用错误")["status"] == "fail"

    good = _note(
        "算法公开便于研究漏洞，但没有发现漏洞不能证明绝对安全。"
        "SM4 是中国商用密码的分组算法。"
        "古典密码曾依赖算法保密；如果将移位规则公开，攻击者容易穷举密钥。"
    )
    assert not note_factual_conflicts(good, sources)


def test_early_crypto_source_claims_do_not_invert_cause_or_overstate_security():
    from pku_sync.note_quality import note_factual_conflicts

    sources = [
        {"title": "ch02-古典密码.pdf", "locator": "第 11 页", "status": "readable",
         "text": "密码体制是五元组(P,C,K,E,D)，任意 k 有加密算法和解密算法。"},
        {"title": "ch02-古典密码.pdf", "locator": "第 36 页", "status": "readable",
         "text": "代替密码按字符替换；置换密码保留字符但打乱顺序。"},
    ]
    wrong = _note(
        "密钥是推导算法的关键。\n\n"
        "国家军事领域要求算法保密，以确保更高的安全等级。\n\n"
        "凯撒密码等古典算法因规则简单且未公开，极易被穷举破解。\n\n"
        "现代密码则通常按字节进行处理（课件：ch02-古典密码.pdf，第 36 页）。"
    )
    issues = note_factual_conflicts(wrong, sources)
    assert len(issues) == 4
    assert any("密钥选择或控制" in issue for issue in issues)
    assert any("军事算法保密策略" in issue for issue in issues)
    assert any("穷举来自密钥空间小" in issue for issue in issues)
    assert any("第 36 页" in issue for issue in issues)

    correct = _note(
        "密钥控制已有的加密算法；不能从一个密钥推导出算法设计。\n\n"
        "老师提到军事领域通常保密算法，但并未由此证明安全性更高。\n\n"
        "凯撒密码可被穷举，因为位移密钥只有少数可能。\n\n"
        "课件第 36 页讨论字符代替与置换；老师将字符类比为一个字节。"
    )
    assert note_factual_conflicts(correct, sources) == []


def test_modern_byte_claim_gate_requires_the_actual_wrong_page_citation():
    from pku_sync.note_quality import note_factual_conflicts

    source = [{"title": "ch02-古典密码.pdf", "locator": "第 36 页",
               "status": "readable", "text": "基于字符的代替和置换密码"}]
    spoken_only = _note("老师说在本例中可以把字符看作一个字节。")
    assert note_factual_conflicts(spoken_only, source) == []
    uncited = _note("现代密码通常按字节进行处理。")
    assert note_factual_conflicts(uncited, source) == []

    same_claim_new_wording = _note(
        "现代密码一般以字节为单位进行处理（课件：ch02-古典密码.pdf，第 36 页）。"
    )
    assert any("第 36 页" in issue for issue in
               note_factual_conflicts(same_claim_new_wording, source))

    byte_source = [{**source[0], "text": "现代密码以字节为单位处理"}]
    assert note_factual_conflicts(same_claim_new_wording, byte_source) == []


def test_late_lecture_kasiski_gate_uses_slide_key_length_scope():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 119 页",
              "status": "readable", "text": "一般来说密码分析家只考虑4个字母以上的密钥长度"}]
    wrong = _note("卡契斯基测试法通过重复片段的间距猜测周期。"
                  "一般来说，密码分析家只考虑 4 字母以上的重复串进行分析。")
    assert any("第 119 页" in issue for issue in note_factual_conflicts(wrong, slide))
    assert any("第 119 页" in issue for issue in note_factual_conflicts(
        wrong, [{**slide[0], "status": "partial"}]))
    correct = _note("卡契斯基测试法统计重复串的间距；候选密钥长度通常考虑 4 字母以上。")
    assert note_factual_conflicts(correct, slide) == []
    unrelated = _note("字谜游戏只考虑 4 字母以上的重复串进行分析。")
    assert note_factual_conflicts(unrelated, slide) == []


def test_vernam_and_rotor_names_are_separate_on_classification_slide():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 133 页",
              "status": "readable", "text": "弗纳姆（Vernam）密码；转轮密码机"}]
    wrong = _note("自动密钥密码和纳姆密码转轮机都属于多表代替。")
    assert any("误合并" in issue for issue in note_factual_conflicts(wrong, slide))
    correct = _note("弗纳姆密码和转轮密码机是不同的多表代替方法。")
    assert note_factual_conflicts(correct, slide) == []
    assert note_factual_conflicts(wrong, []) == []


def test_playfair_rules_need_same_pair_separator_when_citing_complete_slide():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 147 页",
              "status": "readable", "text": "Playfair：相同对中的字母加分隔符(如x)；"
                                               "同行取右边；同列取下边；其他取交叉"}]
    wrong = _note("Playfair 加密规则包括：同行取右侧字母，同列取下侧字母，"
                  "其余情况取交叉点字母（课件：ch02-古典密码.pdf，第 147 页）。")
    assert any("漏掉" in issue for issue in note_factual_conflicts(wrong, slide))
    correct = _note("Playfair 遇到同对相同字母先插入 x 分隔；"
                    "其后同行取右侧字母，同列取下侧字母，其余取交叉点字母。")
    assert note_factual_conflicts(correct, slide) == []
    partial_slide = [{**slide[0], "text": "Playfair：同行、同列、交叉"}]
    assert note_factual_conflicts(wrong, partial_slide) == []


def test_cryptology_branches_are_not_attributed_to_symmetric_cipher():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 5 页",
              "status": "readable", "text": "密码学包含密码编码学与密码分析学两个分支"}]
    wrong = _note("对称密码主要分为密码编码和密码分析两个分支。")
    assert any("误归到对称密码" in issue for issue in note_factual_conflicts(wrong, slide))
    correct = _note("密码学包含密码编码学和密码分析学两个分支。"
                    "对称密码算法用同一密钥加密和解密。")
    assert note_factual_conflicts(correct, slide) == []
    assert note_factual_conflicts(wrong, []) == []


def test_polyalphabetic_position_mapping_is_not_multi_character_block():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 38 页",
              "status": "readable", "text": "多字母密码：明文中字符的映射依赖于上下文位置"}]
    wrong = _note("多字母密码相当于古典的分组密码，每次处理多个字母，"
                  "字符映射取决于上下文位置。")
    assert any("第 38 页" in issue for issue in note_factual_conflicts(wrong, slide))
    correct = _note("多字母密码使单个字符的映射依赖上下文位置；"
                    "Playfair 则把两个字母作为一个单元处理。")
    assert note_factual_conflicts(correct, slide) == []
    assert note_factual_conflicts(wrong, []) == []


def test_one_time_pad_theoretical_guarantee_is_not_claimed_as_nonexistence():
    from pku_sync.note_quality import note_factual_conflicts

    slide = [{"title": "ch02-古典密码.pdf", "locator": "第 33 页",
              "status": "readable", "text": "无条件安全：One-time pad"}]
    wrong = _note("一次一密要求密钥与明文等长，现实中传递代价高，"
                  "因此无条件安全仅存在于理论层面。")
    assert any("仅存在于理论" in issue for issue in note_factual_conflicts(wrong, slide))
    correct = _note("一次一密在正确使用时有理论上的完美保密保证；"
                    "密钥生成、保管和分发的成本使大规模使用困难，历史上仍有实际应用。")
    assert note_factual_conflicts(correct, slide) == []
    assert note_factual_conflicts(wrong, []) == []
