"""A rejected old editor result cannot permanently prevent a bounded improved rewrite."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from pku_sync import media


def setup_claim():
    original = '比如智力、人格啊，它是属于正态分布的啊'
    evidence = '我们要测的心理特质，比如智力、人格啊，它是属于正态分布的啊。'
    claim = {'origin': 'transcript', 'display_text': original, 'transcript_excerpt': evidence,
             'evidence_block_id': 'B0001', 'provisional_fragment': True}
    identifier = media._ledger_sentence_id(claim)
    sentence = '智力、人格等属性属于正态分布。'
    check = {'id': identifier, 'sentence': sentence, 'evidence': evidence, 'original': original}
    verdict = {'id': identifier, 'entailed': False, 'qualifiers_preserved': False,
               'unsupported_terms': ['等属性']}
    old_key = hashlib.sha256(json.dumps({'policy': 'sentence-rewrite-retry-v1', 'check': check,
                                         'verdict': verdict}, ensure_ascii=False,
                                        sort_keys=True).encode()).hexdigest()[:24]
    refused = {'schema_version': 1, 'id': identifier, 'sentence': original}
    record = {'sentence_rewrite_candidate': {'schema_version': 1, 'sentences': [
        {'id': identifier, 'sentence': sentence}]}, 'sentence_rewrite_retries': {old_key: refused}}
    return claim, record, old_key, refused, verdict


@pytest.mark.parametrize('fresh_fragment', [False, True])
def test_rejected_fragment_is_retained_but_new_editor_gets_only_one_source_bound_retry(tmp_path, monkeypatch, fresh_fragment):
    claim, record, old_key, refused, verdict = setup_claim()
    proper = '例如，智力和人格属于正态分布。'
    retries = []
    def response(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        if 'claims' in payload:
            return json.dumps(record['sentence_rewrite_candidate'], ensure_ascii=False)
        if 'check' in payload:
            retries.append(payload)
            assert payload['check']['evidence'] == claim['transcript_excerpt']
            assert payload['verdict'] == verdict
            return json.dumps({'schema_version': 1, 'id': verdict['id'],
                'sentence': claim['display_text'] if fresh_fragment else proper}, ensure_ascii=False)
        assert 'checks' in payload
        check = payload['checks'][0]
        assert check['evidence'] == claim['transcript_excerpt']
        accepted = check['sentence'] == proper
        return json.dumps({'schema_version': 1, 'verdicts': [
            {'id': check['id'], 'entailed': True, 'qualifiers_preserved': True,
             'unsupported_terms': []} if accepted else verdict]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', response)
    if fresh_fragment:
        with pytest.raises(media._NoteQualityError, match='自然笔记定向改写未通过本地核验'):
            media._verified_ledger_sentences(None, 'test', [claim], settings=SimpleNamespace(),
                                             record=record, cache_path=tmp_path / 'ledger.json')
        assert 'verified_sentences' not in record
    else:
        result = media._verified_ledger_sentences(None, 'test', [claim], settings=SimpleNamespace(),
                                                  record=record, cache_path=tmp_path / 'ledger.json')
        assert result[0]['display_text'] == proper and result[0]['verified_sentence'] is True
    assert len(retries) == 1, 'Only one bounded new rewrite is allowed'
    assert record['sentence_rewrite_retries'][old_key] == refused, 'Preserve rejected result'


def source_context_fixture():
    # Exact r19 original B0004..B0006, official PDF page 11, and Ca2811b591707.
    blocks = [{'block_id': 'B0004', 'start': 1889.71, 'end': 1907.46, 'text': '那神经干细胞就肯定原始的啊，就是它会演变成各种各样的，最后我们。 所能呃看到的这种神经细胞，所以它是一类前体细胞。 那神经干细胞呢，还有一个呢，就是说它会转变成叫神经祖细胞。 这个祖细胞实际上相当于一个过渡的这么一个细胞的情况。'}, {'block_id': 'B0005', 'start': 1907.65, 'end': 1925.23, 'text': '那由干细胞到祖细胞，然后由这个祖细胞再增殖分化成我们把它叫先是形成叫长神经细胞和长胶质细胞。 那长神经细胞、长胶质细胞就是我们说的神经细胞和胶质细胞的一个前体细胞。 所以它有一个这样一个演变的过程啊。 那早期的观点认为什么呀？'}, {'block_id': 'B0006', 'start': 1925.23, 'end': 1941.65, 'text': '神经干细胞、神经组细胞它是不分的，就是都是一类前体细胞。 那到了成神经细胞、成胶质细胞的时候，它可能会分成不同的类。 比如这张图上你看七八九都属于成神经细胞，但是你一看是不一样的，就它最后会形成各种各样的哈。'}]
    source = {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 11 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '二 神经细胞的发育\n• 神经管细胞：在神经细胞发\n育的初始，围绕神经管壁的\n上皮细胞形成生发层，又称\n套层。套层内的神经干细胞\n转化为神经祖细胞，再增殖、\n分化分别形成成神经细胞和\n成胶质细胞（前体细胞）。\n1. 神经细胞的增殖\n7、8、9成神经细胞，11套层神经元则来自成神经细胞，神经胶质细胞则来自\n成胶质细胞（25万个神经细胞/分钟）', 'status': 'readable'}
    claim = {'text': '由这个祖细胞再增殖分化成我们把它叫先是形成叫长神经细胞和长胶质细胞', 'display_text': '由这个祖细胞再增殖分化成我们把它叫先是形成叫长神经细胞和长胶质细胞', 'translation_status': 'not_needed', 'origin': 'transcript', 'evidence_block_id': 'B0005', 'transcript_excerpt': '然后由这个祖细胞再增殖分化成我们把它叫先是形成叫长神经细胞和长胶质细胞。', 'evidence_start': 1907.65, 'evidence_end': 1925.23, 'source_filename': None, 'source_page': None, 'source_quote': None, 'evidence_group_id': 'G01'}
    return blocks, source, claim


def request_source_packet(payload, row):
    packet = row.get('source_packet')
    if packet and 'block_ids' in packet:
        blocks = {block['block_id']: block for block in payload['source_blocks']}
        pages = {page['id']: page for page in payload['source_pages']}
        return {**{key: value for key, value in packet.items()
                   if key not in {'block_ids', 'source_page_ids'}},
                'blocks': [blocks[identity] for identity in packet['block_ids']],
                'source_pages': [pages[identity] for identity in packet['source_page_ids']]}
    return packet


@pytest.mark.parametrize('refuse_retry', [False, True])
def test_real_r19_sentence_all_stages_share_complete_bound_source_packet(tmp_path, monkeypatch, refuse_retry):
    import inspect
    blocks, source, claim = source_context_fixture()
    packets = []
    calls = []
    initial = '由这个祖细胞再增殖分化，先形成长神经细胞和长胶质细胞。'
    revised = '神经祖细胞增殖分化形成成神经细胞和成胶质细胞。'
    def response(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        row = (payload['claims'][0] if 'claims' in payload else
               payload['check'] if 'check' in payload else payload['checks'][0])
        packet = request_source_packet(payload, row)
        assert packet is not None, 'Actual r19 rewrite/verifier/retry must receive complete current source context'
        assert packet['blocks'] == blocks
        assert packet['source_pages'][0]['text'] == source['text']
        assert packet['source_pages'][0]['locator'] == source['locator']
        assert packet['block_id'] == claim['evidence_block_id']
        packets.append(packet)
        if 'claims' in payload:
            calls.append('rewrite')
            return json.dumps({'schema_version': 1, 'sentences': [
                {'id': row['id'], 'sentence': initial, 'source_page_id': None}]}, ensure_ascii=False)
        if 'check' in payload:
            calls.append('retry')
            assert '课件' in system and '限定' in system
            return json.dumps({'schema_version': 1, 'id': row['id'],
                'sentence': initial if refuse_retry else revised,
                'source_page_id': packet['source_pages'][0]['id']}, ensure_ascii=False)
        calls.append('verify')
        passed = row['sentence'] == revised
        return json.dumps({'schema_version': 1, 'verdicts': [
            {'id': row['id'], 'entailed': passed, 'qualifiers_preserved': passed,
             'unsupported_terms': [] if passed else ['先形成长神经细胞和长胶质细胞']}]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', response)
    context = {'evidence_blocks': blocks, 'sources': [source]} if 'evidence_blocks' in inspect.signature(media._verified_ledger_sentences).parameters else {}
    record = {}
    if refuse_retry:
        with pytest.raises(media._NoteQualityError, match='定向改写未通过本地核验'):
            media._verified_ledger_sentences(None, 'test', [claim], settings=None,
                record=record, cache_path=tmp_path/'ledger.json', **context)
        assert 'verified_sentences' not in record
        assert calls == ['rewrite', 'verify', 'retry']
    else:
        result = media._verified_ledger_sentences(None, 'test', [claim], settings=None,
            record=record, cache_path=tmp_path/'ledger.json', **context)
        assert result[0]['origin'] == 'both'
        assert result[0]['source_filename'] == source['title']
        assert result[0]['source_quote'] == source['text']
        assert result[0]['evidence_start'] == 1907.65
        assert result[0]['evidence_end'] == 1925.23
        assert result[0]['display_text'] == revised
        assert calls == ['rewrite', 'verify', 'retry', 'verify']
        assert all(packet == packets[0] for packet in packets)
        again = media._verified_ledger_sentences(None, 'test', [claim], settings=None,
            record=record, cache_path=tmp_path/'ledger.json', **context)
        assert again == result and len(calls) == 4
        rendered = media._ledger_claim_text(result[0])
        assert '（录像时间：[31:47]–[32:06]）' in rendered
        assert f"（课件：{source['title']}，{source['locator']}）" in rendered
        assert rendered.endswith('。')
        assert media._ledger_render_guard(rendered, result) is None
        moved = rendered.replace('[31:47]–[32:06]', '[00:00]–[32:06]')
        assert media._ledger_render_guard(moved, result) == 'renderer_changed_physical_source_citation'
        assert result[0]['text'] == claim['text']
        assert result[0]['transcript_excerpt'] == claim['transcript_excerpt']


@pytest.mark.parametrize('bad', ['unknown-page', 'failed-verdict', 'changed-block'])
def test_complete_source_packet_does_not_authorize_bad_or_false_sentences(tmp_path, monkeypatch, bad):
    blocks, source, claim = source_context_fixture()
    sentence = '神经祖细胞增殖分化形成成神经细胞和成胶质细胞。'
    calls = []
    def response(_llm, _model, prompt, _system, _settings):
        payload = json.loads(prompt)
        row = payload['claims'][0] if 'claims' in payload else payload['check'] if 'check' in payload else payload['checks'][0]
        packet = request_source_packet(payload, row)
        calls.append(payload)
        if 'claims' in payload:
            return json.dumps({'schema_version': 1, 'sentences': [{
                'id': row['id'], 'sentence': sentence,
                'source_page_id': 'foreign-page' if bad == 'unknown-page' else packet['source_pages'][0]['id']}]}, ensure_ascii=False)
        if 'check' in payload:
            # An unchanged candidate must fail before a second verifier.
            return json.dumps({'schema_version': 1, 'id': row['id'], 'sentence': sentence,
                'source_page_id': packet['source_pages'][0]['id']}, ensure_ascii=False)
        return json.dumps({'schema_version': 1, 'verdicts': [{
            'id': row['id'], 'entailed': False, 'qualifiers_preserved': False,
            'unsupported_terms': ['形成成神经细胞']}]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', response)
    if bad == 'changed-block':
        blocks[1] = {**blocks[1], 'start': 1908.0}
    record = {}
    with pytest.raises(media._NoteQualityError):
        media._verified_ledger_sentences(None, 'test', [claim], settings=None,
            record=record, cache_path=tmp_path/'ledger.json', evidence_blocks=blocks,
            sources=[source], fallbacks=[])
    assert 'verified_sentences' not in record
    assert len(calls) == {'unknown-page': 1, 'failed-verdict': 3, 'changed-block': 0}[bad]


def test_complete_source_packet_cache_changes_with_adjacent_text_and_model(tmp_path, monkeypatch):
    blocks, source, claim = source_context_fixture()
    calls = []
    def response(_llm, _model, prompt, _system, _settings):
        payload = json.loads(prompt)
        calls.append(payload)
        row = payload['claims'][0] if 'claims' in payload else payload['checks'][0]
        if 'claims' in payload:
            return json.dumps({'schema_version': 1, 'sentences': [{
                'id': row['id'], 'sentence': claim['display_text'], 'source_page_id': None}]}, ensure_ascii=False)
        return json.dumps({'schema_version': 1, 'verdicts': [{
            'id': row['id'], 'entailed': True, 'qualifiers_preserved': True,
            'unsupported_terms': []}]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', response)
    record = {}
    for model, context in [('model-one', blocks), ('model-two', blocks),
                           ('model-two', [{**blocks[0], 'text': blocks[0]['text'] + '相邻原文变化。'}, *blocks[1:]])]:
        media._verified_ledger_sentences(None, model, [claim], settings=None,
            record=record, cache_path=tmp_path/'ledger.json', evidence_blocks=context, sources=[source])
    assert len(calls) == 6
    assert len(record['sentence_verification_batches']) == 3


@pytest.mark.parametrize('final_supported', [True, False])
def test_plain_organizer_preserves_relationship_and_replay_notice(tmp_path, monkeypatch, final_supported):
    """Offline provider text exercises persistence, not semantic accuracy."""
    from pku_sync import note_editor, transcript_notes
    correct = '课堂辨认树突与细胞体的连接位置。'
    transcript = {'segments': [{'start': 60, 'end': 120, 'text': correct}]}
    source = {'title': '本讲.pdf', 'locator': '第 1 页', 'status': 'readable', 'text': correct}
    output = correct if final_supported else '课堂比较两种结构的连接位置。连接关系需结合原录像核对。'
    calls = []
    def provider(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt); calls.append(payload)
        assert payload['transcript'] == transcript['segments'] and 'courseware' not in payload
        assert system == transcript_notes.SYSTEM
        return output
    monkeypatch.setattr(media, '_recovery_llm', provider)
    monkeypatch.setattr(media, '_recover_chapter_with_ledger', lambda *a, **k: pytest.fail('retired recovery'))
    target = tmp_path/'notes.md'
    settings = SimpleNamespace(platform_token='test', openai_api_key='', notes_model='test')
    assert media.write_notes(transcript, [], target, settings, '课程', source_context=[source]) == target
    report = json.loads(note_editor.report_path(target).read_text('utf-8'))
    assert report['status'] == 'organized' and report['content_complete'] is None
    assert target.read_text('utf-8') == '# 课程\n\n' + output + '\n'
    records = [json.loads(p.read_text('utf-8')) for p in
        (tmp_path/'.notes-parts'/transcript_notes.POLICY).glob('*.json')]
    assert len(records) == 1 and records[0]['response'] == output
    assert media.write_notes(transcript, [], target, settings, '课程', source_context=[source]) == target
    assert len(calls) == 1 and note_editor.review_record_issues(target, transcript, [source]) == []


@pytest.mark.parametrize('failure_kind', ['missing-field', 'invalid-json', 'invalid-type'])
def test_literal_json_output_is_preserved_without_format_recovery(tmp_path, monkeypatch, failure_kind):
    from pku_sync import note_editor, transcript_notes
    text = '课堂辨认树突与细胞体的连接位置。'
    transcript = {'segments': [{'start': 60, 'end': 120, 'text': text}]}
    raw = {'missing-field': json.dumps({'note': text, 'uncertainties': []}),
           'invalid-json': '{"note":',
           'invalid-type': json.dumps({'note': text, 'complete': 'true', 'uncertainties': []})}[failure_kind]
    calls = []
    monkeypatch.setattr(media, '_recovery_llm', lambda *args: calls.append(args) or raw)
    monkeypatch.setattr(media, '_recover_chapter_with_ledger', lambda *a, **k: pytest.fail('retired paid recovery'))
    settings = SimpleNamespace(platform_token='test', openai_api_key='', notes_model='test')
    target = tmp_path/'notes.md'
    for _ in range(2):
        media.write_notes(transcript, [], target, settings, '课程')
    assert len(calls) == 1 and target.read_text('utf-8') == '# 课程\n\n' + raw + '\n'
    records = [json.loads(p.read_text('utf-8')) for p in
        (tmp_path/'.notes-parts'/transcript_notes.POLICY).glob('*.json')]
    assert len(records) == 1 and records[0]['response'] == raw
    assert note_editor.review_record_issues(target, transcript) == []


def test_original_r19_speech_delivered_once_and_full_references_preserved(tmp_path, monkeypatch):
    import copy
    from pku_sync import note_editor, transcript_notes
    blocks, original_source, _claim = source_context_fixture()
    transcript = {'segments': blocks}
    sources = [{**original_source, 'locator': f'第 {index} 页',
                'text': original_source['text'] * 18 + f'完整页尾身份{index}'} for index in range(1, 10)]
    originals = copy.deepcopy(sources)
    calls = []
    def provider(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt); calls.append(payload)
        assert payload['transcript'] == blocks and 'courseware' not in payload
        return '神经祖细胞增殖分化形成成神经细胞和成胶质细胞。'
    monkeypatch.setattr(media, '_recovery_llm', provider)
    monkeypatch.setattr(media, '_recover_chapter_with_ledger', lambda *a, **k: pytest.fail('legacy source cropping'))
    target = tmp_path/'notes.md'
    media.write_notes(transcript, [], target,
                      SimpleNamespace(platform_token='test', openai_api_key='', notes_model='test'),
                      '课程', source_context=sources)
    assert len(calls) == 1 and sources == originals
    report = json.loads(note_editor.report_path(target).read_text('utf-8'))
    assert report['sources_sha256'] == note_editor.fingerprint(originals)
    assert report['references_role'] == 'preserved-for-separate-term-review'
    assert note_editor.review_record_issues(target, transcript, sources) == []


def actual_r20_cross_claim_source_fixture():
    # Frozen installed48 response and real per-claim source rows; not an accepted note.
    claim = {'text': '中枢神经系统的发育，包括这样几个大的阶段', 'display_text': '中枢神经系统的发育，包括这样几个大的阶段', 'translation_status': 'not_needed', 'origin': 'transcript', 'evidence_block_id': 'B0020', 'transcript_excerpt': '这是这个是要知道的，就是我们后面在讲的时候可能，呃，这相对于把后面的一个内容做一个概括，就整体来说是中枢神经系统的发育，包括这样几个大的阶段哈。 第一个呢，后面会提到，就是我们说从原肠、原肠胚，然后三胚层。', 'evidence_start': 550.56, 'evidence_end': 569.28, 'source_filename': None, 'source_page': None, 'source_quote': None, 'evidence_group_id': 'G02'}
    blocks = [{'block_id': 'B0019', 'start': 542.12, 'end': 550.28, 'text': '那我们说呢，呃，整体哈，就把发育呢，呃，或者是中枢神经系统发育，可以简单的归结这样几个阶段哈。'}, {'block_id': 'B0020', 'start': 550.56, 'end': 569.28, 'text': '这是这个是要知道的，就是我们后面在讲的时候可能，呃，这相对于把后面的一个内容做一个概括，就整体来说是中枢神经系统的发育，包括这样几个大的阶段哈。 第一个呢，后面会提到，就是我们说从原肠、原肠胚，然后三胚层。'}, {'block_id': 'B0021', 'start': 569.55, 'end': 583.24, 'text': '那会分成内中外三个胚层，这个后面还会有介绍哈。 然后其中我们所主要关注的在神经系统发育这一块，主要是外胚层，也就外胚层，因为它主要演变成神经和皮肤的这一部分结构哈。'}]
    sources = [{'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 2 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '• 人的中枢神经系统 （脑和脊髓 ）凭借大约 140亿\n（860亿）个神经细胞 （神经元 ）和至少 100万亿\n（数千万亿)个神经细胞与靶细胞的专一性突触联系 ，\n担负其接受、加工、储存并准确地传递复杂信息的功\n能。中枢神经系统复杂高级功能的基础是其高度复杂\n而精细的结构，它是发育时期的一个高度复杂的动力\n学过程的产物。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 22 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '神经系统发育的很多特性中 ，最为神奇的或许是生长轴突穿过复杂的细胞\n领地找到几个毫米或几个厘米之外与之匹配的突触后靶细胞。\n现在已经知道轴突的生长能力是生长锥 —— 生长轴突顶端的一个特殊结构\n所具有的特性。生长锥是运动活跃的结构 ，不停地探测细胞外环境并对局\n部信号作出反应，表现为生长速度和方向的改变 。生长锥由扁平的板层形\n伪足及其丝状伪足组成 。丝状伪足一会伸出，一会缩回消失，如同伸出的\n手指触摸环境以获取“感觉”并决定其何去何从。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 51 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '•细胞凋亡主要发生在发育阶段，成年期出现的细\n胞凋亡则可能有新的动因参与。在神经系统的发\n育过程中，通过细胞凋亡可剪除多余和表型异常\n的神经细胞，优选或清除细胞系，塑造突触的模\n式，为形成复杂的神经网络提供更多的选择机会\n和可塑性。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 50 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '五 神经系统发育过程中的细胞凋亡\n• 细胞凋亡是细胞程序性死亡，是不同于细胞坏死的另\n一种细胞死亡方式。通过基因调控，细胞内发生的一\n系列级联反应使细胞凋亡。\n• 细胞凋亡与细胞的增殖、分化、衰老起着相互制约和\n平衡的作用。在神经系统发育的过程中，脑内大约有\n15-85%的细胞发生凋亡。 例如哺乳动物胚胎脑细胞\n大约有50%在出生前凋亡。凋亡的程度因物种或脑区\n的不同而异。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 53 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '但是近20年来，越来越多的实验表明 ，动物和人类大脑的神经元可\n以再生，而且这一能力可能可以持续终生。\n近二三十年出现了一些新的实验技术 ，可以应用放射性元素和核苷\n酸类似物标记处于分裂相的细胞 ，由此在许多成年动物中都发现了\n新生神经元（海马）的证据。\n随后，成人大脑存在新生神经元的证据也开始得到报道 。首先是美\n国和瑞典两国的科学家在 1998年用核苷酸类似物标记的方法 ，在成\n人的海马中发现了幼稚神经元；随后 ，瑞典研究者在 2006年用碳14\n追踪的方法计算出成人的海马每天可以产生700个神经元；2010年，\n德国科学家也用抗体标记的方法发现了成人海马中的新生神经元。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 13 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '神经元出生期的研究表明 ，具有特定细胞类型和神经联\n系模式的每个脑结构是由一个特定发育时间内出生的许\n多细胞聚集而成的；\n神经元表型的多样性：基因与特定脑区微环境中其他细\n胞的相互作用（移植胚胎的小块脑区到宿主动物）\n分裂后的成神经细胞最终的准确定位特别重要：发育中\n的突触前和突触后成分必须在准确的时间并在准确的位\n置才能建立起具有信息传递功能的神经元间的准确联系 。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 23 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '神经系统有两类细胞粘连分子：神经元-胶质细胞粘连分子和神经细胞粘连分子，二者属\n于同源性分子。前者主要见于中枢和外周的胶质细胞，后者主要见于生长阶段的轴突。\n在轴突的生长过程中：生长锥上的细胞粘连分子与星型胶质细胞或雪旺细胞的同源分子\n结合，从而沿着这些细胞延伸。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 21 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '• 新生儿神经元的突起少，2岁时突起和突触数量达到\n成人水平，4-10岁突触数超过成人水平 ，16岁减至\n成人水平。修剪\n• 神经元的增殖、分化和突触结构和功能终身都具有\n可塑性，只是可塑性会随着年龄的增加和神经系统\n的成熟而越来越小。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 57 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '七 后天刺激对神经系统发育的影响\n• 遗传基因是决定神经系统发育的主因 。但遗传物质只\n给出了神经系统发育的框架。在神经系统的发育过程中，\n特别是出生后的发育阶段，神经系统的发育要受到各种\n因素的影响，其中外界后天的刺激和学习对突触结构和\n脑的发育有着巨大的影响。\n• 出生后脑继续发育，如果考虑智力的形成，则后天的\n影响更明显。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 11 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '二 神经细胞的发育\n• 神经管细胞：在神经细胞发\n育的初始，围绕神经管壁的\n上皮细胞形成生发层，又称\n套层。套层内的神经干细胞\n转化为神经祖细胞，再增殖、\n分化分别形成成神经细胞和\n成胶质细胞（前体细胞）。\n1. 神经细胞的增殖\n7、8、9成神经细胞，11套层神经元则来自成神经细胞，神经胶质细胞则来自\n成胶质细胞（25万个神经细胞/分钟）', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 52 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '六 神经干细胞\n• 神经科学界传统上认为，神经元是典型的减数分裂后\n细胞，形成神经元后细胞不再分裂。神经元数量在发\n育过程中被“固定”，成熟后中枢神经系统已完全\n“定型”。\n• 21世纪初，德国著名的神经病学家费尔德曾经断言 ，\n受损伤的中枢神经系统的神经元没有再生能力 。这种\n“受损脑神经元不可逆”的理论统治了一个世纪。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 7 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '一 神经板的发育；\n二 神经细胞的发育；\n三 脊髓的发育；\n四 脑的发育；\n五 神经系统发育过程中的细胞凋亡；\n六 神经再生；\n七 后天刺激对神经系统发育的影响', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 20 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '4. 突触形成与建立\n• 突触形成过程中 ，神经元的迁移受周围胶质细胞 、局部\n环境的物质和靶细胞的影响；突触后细胞诱导突触前细\n胞轴突的生长和迁移；突触的形成受突触前 、后细胞相\n互识别的影响；突触前细胞对突触后细胞有支配和营养\n作用（相互作用）。\n• 在整个生存期内 ，神经元连接的类型和功效受后天刺激\n的影响，是一个不断增殖和精简的过程 ，遵循“用存不\n用则废”的原则。', 'status': 'readable'}, {'title': '00-5cb79580797c-20260930中枢神经系统发育.pdf', 'locator': '第 3 页', 'path': 'materials\\.lecture-sources\\c0d90f5b60307815bf5cca6a\\00-5cb79580797c-20260930中枢神经系统发育.pdf', 'text': '• 外胚层分化为神经板和神经管;\n• 细胞的增殖和迁移;\n• 细胞群落或神经核团的形成;\n• 神经细胞轴突的生长;\n• 细胞间突触联系的建立;\n• 复杂神经回路的构建及其生后早期的修饰。', 'status': 'readable'}]
    returned = {'id': 'C4618c5a78931', 'sentence': '中枢神经系统的发育，包括这样几个大的阶段。', 'source_page_id': 'Saee71f107e601c53601c'}
    allowed = ['S054c88fd8adcb175bf97', 'S5b8106a4505daf4e4b84', 'Se34c06951614a5c48dbb', 'S57a9412679e8460fbc65']
    return claim, blocks, sources, returned, allowed


@pytest.mark.parametrize('result', ['supported', 'false', 'bad-id', 'same-sentence', 'unknown'])
def test_actual_r20_cross_claim_id_uses_one_bound_repair_then_strict_verifier(tmp_path, monkeypatch, result):
    claim, blocks, sources, returned, allowed = actual_r20_cross_claim_source_fixture()
    calls = []
    revised = '中枢神经系统的发育包括几个大的阶段。'
    def provider(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        calls.append(payload)
        if 'claims' in payload:
            row = payload['claims'][0]
            assert row['id'] == returned['id']
            assert set(row['source_packet']['source_page_ids']) == set(allowed)
            assert returned['source_page_id'] not in allowed
            assert any(page['id'] == returned['source_page_id'] for page in [
                {'id': 'S' + hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]}
                for source in sources]), 'Returned ID is real but belongs to another claim packet'
            return json.dumps({'schema_version': 1, 'sentences': [returned]}, ensure_ascii=False)
        if 'check' in payload:
            assert payload['trigger'] == 'local_source_binding_error'
            assert payload['binding_error']['returned'] == returned['source_page_id']
            assert set(payload['binding_error']['allowed']) == set(allowed)
            assert 'verdict' not in payload, 'Local ID validation must never impersonate a model verdict'
            if result == 'unknown':
                raise TimeoutError('mock response lost after one request')
            return json.dumps({'schema_version': 1, 'id': returned['id'],
                               'sentence': returned['sentence'] if result == 'same-sentence' else revised,
                               'source_page_id': returned['source_page_id'] if result == 'bad-id' else None}, ensure_ascii=False)
        assert 'checks' in payload
        assert payload['checks'][0]['source_page_id'] is None
        return json.dumps({'schema_version': 1, 'verdicts': [{
            'id': returned['id'], 'entailed': result == 'supported',
            'qualifiers_preserved': result == 'supported',
            'unsupported_terms': [] if result == 'supported' else ['来源条件未获支持']}]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', provider)
    record = {}
    kwargs = dict(settings=None, record=record, cache_path=tmp_path/'r20-ledger.json',
                  evidence_blocks=blocks, sources=sources)
    if result == 'supported':
        output = media._verified_ledger_sentences(None, 'qwen3.5-flash', [claim], **kwargs)
        assert output[0]['display_text'] == revised
        assert output[0]['verified_sentence'] is True
        assert output[0]['origin'] == 'transcript'
    else:
        with pytest.raises((media._NoteQualityError, TimeoutError)):
            media._verified_ledger_sentences(None, 'qwen3.5-flash', [claim], **kwargs)
        count = len(calls)
        with pytest.raises(media._NoteQualityError):
            media._verified_ledger_sentences(None, 'qwen3.5-flash', [claim], **kwargs)
        assert len(calls) == count, 'Lost/failed bound correction cannot be replayed or get a second paid attempt'
        assert record.get('status') != 'sentences_verified'
    assert sum('check' in call for call in calls) == 1
    assert sum('checks' in call for call in calls) == (1 if result in {'supported', 'false'} else 0)
    assert record['sentence_rewrite_candidate']['sentences'] == [returned]
    assert all(error.get('trigger') == 'local_source_binding_error'
               and 'verdict' not in error for error in record['sentence_source_binding_errors'].values())


def actual_r20_allowed_but_unsupported_page_fixture():
    claim = {'text': '胶质细胞的数量其实比神经元的数量还要多', 'display_text': '胶质细胞的数量其实比神经元的数量还要多', 'translation_status': 'not_needed', 'origin': 'transcript', 'evidence_block_id': 'B0012', 'transcript_excerpt': '那我们说呢，人的中神经系统内，我们说神经细胞的数量非常大，然后神经细胞和。 神经细胞之间联系突出的数量就更多，包括之前我们在讲嗯第二堂课介绍时候也提过，胶质细胞的数量其实比神经元的数量还要多，对吧？', 'evidence_start': 438.04, 'evidence_end': 453.9, 'source_filename': None, 'source_page': None, 'source_quote': None, 'evidence_group_id': 'G01'}
    blocks = [{'block_id': 'B0011', 'start': 421.84, 'end': 437.8, 'text': '就是我们说强调这个量是非常大的哈，那当然后面可能我们再发现可能后面还会再变化啊，这都不好说。 所以我是把原来的加上了留着了，然后后面括号里也给补充了最新的这个相关的一些数据的情况哈。'}, {'block_id': 'B0012', 'start': 438.04, 'end': 453.9, 'text': '那我们说呢，人的中神经系统内，我们说神经细胞的数量非常大，然后神经细胞和。 神经细胞之间联系突出的数量就更多，包括之前我们在讲嗯第二堂课介绍时候也提过，胶质细胞的数量其实比神经元的数量还要多，对吧？'}, {'block_id': 'B0013', 'start': 453.9, 'end': 470.65, 'text': '所以整个包括细胞的数量，包括我们说细胞细胞之间的这种实现信息传递突出的量都是非常大的，对吧？ 而且这么大的量的细胞和细胞之间的联系，它还要实现啊，就是我们说的加工、存储、传递。'}]
    returned = {'id': 'Ccc736949b98c', 'sentence': '胶质细胞的数量其实比神经元的数量还要多。', 'source_page_id': 'S57a9412679e8460fbc65'}
    return claim, blocks, returned


def test_actual_r20_real_allowed_page_without_quantity_support_remains_semantically_false(tmp_path, monkeypatch):
    claim, blocks, returned = actual_r20_allowed_but_unsupported_page_fixture()
    sources = actual_r20_cross_claim_source_fixture()[2]
    calls = []
    def provider(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        calls.append(payload)
        if 'claims' in payload:
            assert returned['source_page_id'] in payload['claims'][0]['source_packet']['source_page_ids']
            return json.dumps({'schema_version': 1, 'sentences': [returned]}, ensure_ascii=False)
        if 'check' in payload:
            assert 'verdict' in payload and 'binding_error' not in payload
            return json.dumps({'schema_version': 1, 'id': returned['id'],
                'sentence': '胶质细胞的数量比神经元的数量更多。', 'source_page_id': None}, ensure_ascii=False)
        assert 'checks' in payload
        return json.dumps({'schema_version': 1, 'verdicts': [{
            'id': returned['id'], 'entailed': False, 'qualifiers_preserved': False,
            'unsupported_terms': ['课件数量关系没有来源支持']}]}, ensure_ascii=False)
    monkeypatch.setattr(media, '_recovery_llm', provider)
    record = {}
    with pytest.raises(media._NoteQualityError, match='语义核验未通过'):
        media._verified_ledger_sentences(None, 'qwen3.5-flash', [claim], settings=None,
            record=record, cache_path=tmp_path/'allowed-quantity.json', evidence_blocks=blocks, sources=sources)
    assert sum('check' in payload for payload in calls) == 1
    assert sum('checks' in payload for payload in calls) == 2
    assert not record.get('sentence_source_binding_errors')
    assert record.get('status') != 'sentences_verified'


def test_actual_r21_packet_initial_and_only_retry_receive_natural_writing_duty(tmp_path, monkeypatch):
    # The r21 first claim is the same real r20 source-bound claim. Unchanged
    # wording plus null remains rejected; prompt coverage cannot bypass it.
    claim, blocks, sources, returned, allowed = actual_r20_cross_claim_source_fixture()
    systems = []
    record = {}

    def response(_llm, _model, prompt, system, _settings):
        payload = json.loads(prompt)
        systems.append(system)
        if 'claims' in payload:
            assert set(payload['claims'][0]['source_packet']['source_page_ids']) == set(allowed)
            return json.dumps({'schema_version': 1, 'sentences': [returned]}, ensure_ascii=False)
        assert payload['trigger'] == 'local_source_binding_error'
        assert 'verdict' not in payload
        return json.dumps({'schema_version': 1, 'id': returned['id'],
                          'sentence': returned['sentence'], 'source_page_id': None}, ensure_ascii=False)

    monkeypatch.setattr(media, '_recovery_llm', response)
    with pytest.raises(media._NoteQualityError, match='自然笔记定向改写未通过本地核验'):
        media._verified_ledger_sentences(None, 'qwen3.5-flash', [claim], settings=None,
            record=record, cache_path=tmp_path / 'r21-ledger.json',
            evidence_blocks=blocks, sources=sources)
    assert len(systems) == 2
    for system in systems:
        assert '中文课堂笔记编辑' in system
        assert '自然完整' in system and '独立' in system
        assert '去除口语赘词' in system
        assert '数字' in system and '限定' in system and '因果' in system
        assert '不得增删事实' in system and '任意换字' in system
    assert 'verified_sentences' not in record

