from pku_sync.note_alignment import unspoken_slide_quote_issues


BRUCE = {
    "title": "ch01-绪论.pdf",
    "locator": "第 11 页",
    "text": "Bruce Schneier 安全性是一条链，其可靠性程度取决于链中最薄弱的环节（木桶理论）。安全性是一个过程，而不是产品。",
    "status": "readable",
}
CIA = {
    "title": "ch01-绪论.pdf",
    "locator": "第 84 页",
    "text": "安全的信息交换应满足：机密性、完整性、可用性、真实性、不可否认性。",
    "status": "readable",
}
SERVICES = {
    "title": "ch01-绪论.pdf",
    "locator": "第 100 页",
    "text": "五大类安全服务（鉴别、访问控制、机密性、完整性、抗否认）；八类安全机制。",
    "status": "readable",
}


def test_named_quote_from_prior_lecture_is_reported() -> None:
    summary = (
        "引用了 Bruce Schneier 的观点：安全性是一条链，其可靠性取决于最薄弱的环节"
        "（课件：ch01-绪论.pdf，第 11 页）。"
    )
    transcript = "今天讲古典密码。上堂课回顾 CIA、五大类安全服务和八类安全机制。"
    issues = unspoken_slide_quote_issues(summary, transcript, [BRUCE, CIA, SERVICES])
    assert len(issues) == 1
    assert issues[0]["person"] == "Bruce Schneier"
    assert issues[0]["source_locator"] == "第 11 页"


def test_real_style_attribution_one_sentence_before_page_citation() -> None:
    paragraph = (
        "关于系统安全性的认知，引用了 Bruce Schneier 的观点：安全性是一条链，其可靠性取决于最薄弱的环节"
        "（木桶理论）；安全性是一个过程而非产品，必须渗透到系统的组件和连接中。"
        "没有系统是完美的，也没有技术是灵丹妙药（课件：ch01-绪论.pdf，第 11 页）。"
        "需注意，柯克霍夫原则强调算法公开并不等同于系统不安全。"
    )
    transcript = "上堂课回顾 CIA、五大类安全服务和八类安全机制；今天讲古典密码。"
    issues = unspoken_slide_quote_issues(paragraph, transcript, [BRUCE, CIA, SERVICES])
    assert len(issues) == 1
    assert "最薄弱的环节" in issues[0]["sentence"]
    assert "没有系统是完美的" in issues[0]["sentence"]
    assert "柯克霍夫" not in issues[0]["sentence"]


def test_recap_slides_without_named_quotations_are_not_reported() -> None:
    summary = (
        "回顾 CIA 的机密性、完整性、可用性（课件：ch01-绪论.pdf，第 84 页）。"
        "老师又提到五大类安全服务和八类安全机制（课件：ch01-绪论.pdf，第 100 页）。"
    )
    transcript = "上堂课讲了 CIA。五大类安全服务，八类安全机制，今天进入古典密码。"
    assert not unspoken_slide_quote_issues(summary, transcript, [BRUCE, CIA, SERVICES])


def test_named_quote_is_allowed_when_author_or_quote_is_spoken() -> None:
    summary = "Bruce Schneier 认为安全性是一条链（课件：ch01-绪论.pdf，第 11 页）。"
    assert not unspoken_slide_quote_issues(summary, "Bruce Schneier 说安全性是一条链。", [BRUCE])
    assert not unspoken_slide_quote_issues(summary, "安全性是一条链，这点先回顾。", [BRUCE])


def test_draft_cannot_validate_its_own_unspoken_quote() -> None:
    # The page truly contains the quote, but it must not certify that this
    # week's teacher actually raised it.
    draft = "引用 Bruce Schneier 的观点：安全性是一条链（课件：ch01-绪论.pdf，第 11 页）。"
    assert unspoken_slide_quote_issues(draft, "本讲讨论凯撒密码。", [BRUCE])


def test_alias_can_mark_a_transliterated_spoken_name() -> None:
    summary = "Bruce Schneier 认为安全性是一条链（课件：ch01-绪论.pdf，第 11 页）。"
    assert not unspoken_slide_quote_issues(
        summary, "施奈尔提到了系统安全。", [BRUCE],
        person_aliases={"Bruce Schneier": ["施奈尔"]},
    )
