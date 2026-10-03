from pku_sync.lecture_units import build_unit_packet, format_unit_context


def _page(number: int, text: str, *, projected: bool = False) -> dict:
    row = {
        "title": "lecture.pdf",
        "locator": f"第 {number} 页",
        "text": text,
        "status": "readable",
    }
    if projected:
        row["projected"] = True
    return row


def test_unit_packet_preserves_transcript_blocks_and_confirmed_slide():
    packet = build_unit_packet(
        0,
        {
            "start": 10,
            "end": 80,
            "text": "[00:10] 讲解 RSA 公钥算法。",
            "evidence_blocks": [
                {"block_id": "B0001", "start": 10, "end": 20,
                 "text": "讲解 RSA 公钥算法。"},
            ],
        },
        [{"title": "lecture.pdf", "page": 30, "certain": True}],
        [_page(30, "RSA 公钥算法。"), _page(31, "后续内容。")],
    )
    assert packet["unit_id"] == "U0001"
    assert packet["transcript"]["evidence_blocks"][0]["id"] == "B0001"
    assert packet["slides"]["confirmed_on_screen"][0]["page"] == "第 30 页"
    assert packet["slides"]["confirmed_on_screen"][0]["confirmed_on_screen"] is True
    assert packet["coverage"]["confirmed_slide_count"] == 1
    assert packet["writing_policy"]["transcript_authority"] == "speech_examples_coursework"


def test_unit_packet_deduplicates_pages_and_context_is_json():
    packet = build_unit_packet(
        3,
        {"start": 0, "end": 1, "text": "内容", "evidence_blocks": []},
        [],
        [_page(2, "内容"), _page(2, "重复内容"), _page(3, "另一个页")],
    )
    assert packet["unit_id"] == "U0004"
    assert [row["page"] for row in packet["slides"]["candidates"]] == [
        "第 2 页", "第 3 页",
    ]
    assert '"schema_version":1' in format_unit_context(packet)
