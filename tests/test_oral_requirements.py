from pku_sync.oral_requirements import extract_oral_requirements


def _s(start, text, duration=2):
    return {"start": start, "end": start + duration, "text": text}


def test_question_reply_is_a_lead_but_not_a_formal_assignment():
    segments = [
        _s(100, "咱们作业什么时候开始布置？"),
        _s(102, "呃。", 1),
        _s(103, "今天就布置。"),
    ]
    leads = extract_oral_requirements(segments)
    assert len(leads) == 1
    assert leads[0]["kind"] == "assignment_announcement"
    assert leads[0]["status"] == "confirmed"
    assert leads[0]["evidence"] == [
        {"start": 100.0, "end": 102.0, "quote": "咱们作业什么时候开始布置？"},
        {"start": 103.0, "end": 105.0, "quote": "今天就布置。"},
    ]
    assert leads[0]["deadline_spoken"] is None
    assert leads[0]["formal_assignment_verified"] is False
    assert leads[0]["speaker_verified"] is False


def test_written_submission_and_tentative_experiment_are_distinct():
    utterance = "对，就是书面作业在教学网交，然后实验作业吧，我找公司用实验平台，到时候协商。"
    leads = extract_oral_requirements([_s(30, utterance)])
    assert [(lead["kind"], lead["status"], lead["location_spoken"])
            for lead in leads] == [
                ("submission_location", "confirmed", "教学网"),
                ("possible_experiment_platform", "tentative", "实验平台"),
            ]
    assert all(lead["evidence"][0]["quote"] == utterance for lead in leads)


def test_estimates_preserve_every_quantity_and_do_not_become_requirements():
    text = "书面作业五六次，实验作业三四次，我感觉大概10次吧。"
    leads = extract_oral_requirements([_s(50, text)])
    assert len(leads) == 1
    assert leads[0]["kind"] == "workload_estimate"
    assert leads[0]["status"] == "tentative"
    assert leads[0]["quantities_spoken"] == ["五六次", "三四次", "10次"]


def test_explicit_due_date_is_literal_but_today_assignment_is_not_due_date():
    leads = extract_oral_requirements([
        _s(10, "今天布置作业。"),
        _s(20, "这次作业下周五之前提交。"),
    ])
    assert [(lead["kind"], lead["deadline_spoken"]) for lead in leads] == [
        ("assignment_announcement", None),
        ("spoken_deadline", "下周五"),
    ]
    assert all(lead["formal_assignment_verified"] is False for lead in leads)


def test_due_date_is_the_date_nearest_due_expression():
    leads = extract_oral_requirements([_s(20, "今天布置作业，周五前提交。")])
    deadlines = [lead["deadline_spoken"] for lead in leads if lead["kind"] == "spoken_deadline"]
    assert deadlines == ["周五"]


def test_attendance_obligation_requires_operative_affirmative_wording():
    leads = extract_oral_requirements([
        _s(10, "往年课堂展示要到场。"),
        _s(20, "这次课堂展示必须到场，计入平时成绩。"),
        _s(30, "这次展示不用到场。"),
        _s(40, "展示能不能不来？"),
    ])
    assert len(leads) == 1
    assert leads[0]["kind"] == "attendance_obligation"
    assert leads[0]["status"] == "confirmed"
    assert leads[0]["evidence"][0]["start"] == 20


def test_mentioning_teaching_network_or_student_assertion_is_not_teacher_instruction():
    assert extract_oral_requirements([
        _s(10, "你没有进教学网，是不是过期了？"),
        _s(20, "你今天就布置作业了。"),
        _s(30, "作业是发到教学网？"),
    ]) == []


def test_coursework_location_can_continue_across_short_asr_segments():
    leads = extract_oral_requirements([
        _s(7000, "今天课后作业呢就是。", 4),
        _s(7006, "是在课程作业的那个文件夹下面。", 5),
    ])
    assert len(leads) == 1
    assert leads[0]["kind"] == "submission_location"
    assert leads[0]["location_spoken"] == "课程作业的那个文件夹"
    assert leads[0]["start"] == 7000
    assert leads[0]["end"] == 7011


def test_invalid_segments_are_ignored_without_losing_valid_evidence():
    leads = extract_oral_requirements([
        {"start": "broken", "text": "作业今天布置"},
        {"start": 5, "end": 4, "text": "作业今天布置"},
        _s(6, "作业今天布置。"),
    ])
    assert len(leads) == 1
    assert leads[0]["evidence"][0]["quote"] == "作业今天布置。"
