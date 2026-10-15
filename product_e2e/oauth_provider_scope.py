"""Real visible OAuth scope selection bound to an independently read test root."""
import json
from .oauth_provider_observation import bind_empty_root_label

PARENT = "32091b6f53e180fc8ec8e650b4479f5d"


def root_only_grant(run, provider, responses, independent_parent):
    """Operate current visible controls; fail before granting ambiguous scope."""
    assert str(run.need('learning.notion_test_parent_id')).replace('-', '').lower() == PARENT
    def click(selector, expected):
        provider.wait(lambda: provider.visible(selector))
        assert provider.page.locator(selector).count() == 1
        assert ' '.join(provider.text(selector).split()) == ' '.join(expected.split())
        provider.click(selector)
    provider.wait(lambda: provider.visible('[role="button"].notion-sidebar-switcher:visible'))
    switcher = '[role="button"].notion-sidebar-switcher:visible'
    if 'Di Wu的 Notion' not in provider.text(switcher):
        provider.screenshot('provider-current-workspace-before-switch')
        click(switcher, provider.text(switcher))
        item = '[role="menuitem"]:has-text("Di Wu的 Notion"):visible'
        provider.wait(lambda: provider.visible(item))
        assert provider.page.locator(item).count() == 1
        provider.screenshot('provider-current-workspace-menu')
        click(item, 'D\nDi Wu的 Notion')
    provider.wait(lambda: 'Di Wu的 Notion' in provider.text(switcher))
    click('[role="button"]:text-is("Select pages"):visible', 'Select pages')
    provider.screenshot('provider-current-select-pages')
    click('[role="button"]:has-text("Private"):visible', 'Private')
    # NativeUI.visible deliberately requires one matching element. This is a
    # collection; every checkbox is checked explicitly below before granting.
    provider.wait(lambda: provider.page.locator('input[type="checkbox"]:visible').count() > 0)
    provider.screenshot('provider-current-private-pages')
    labels = [text.strip() for text in provider.page.locator('[role="button"]:visible').all_text_contents()]
    label = bind_empty_root_label(responses, independent_parent, labels, PARENT)
    boxes = provider.page.locator('input[type="checkbox"]:visible')
    assert boxes.count() > 0 and not any(boxes.nth(i).is_checked() for i in range(boxes.count()))
    row = '[role="button"]:has-text("New page"):visible'
    assert provider.page.locator(row).count() == 1
    assert provider.page.locator(row).locator('input[type="checkbox"]').count() == 1
    click(row, label)
    provider.wait(lambda: provider.page.locator(row).locator('input[type="checkbox"]').is_checked())
    selected = provider.page.locator('input[type="checkbox"]:visible').evaluate_all("""nodes=>nodes.map(n=>({
        checked:n.checked,label:(n.closest('[role="button"]')?.innerText||'').trim()}))""")
    assert [x['label'] for x in selected if x['checked']] == [label]
    evidence = run.case_dir / 'provider-root-only-scope.json'
    evidence.write_text(json.dumps({'expected_root_id':PARENT, 'bound_visible_label':label,
        'provider_responses':responses, 'selected_visible_rows':selected,
        'independent_parent_id':independent_parent['id'], 'scope_is_only_required_root':True},
        ensure_ascii=False, indent=2), 'utf-8')
    run.ui_evidence.append(evidence)
    provider.screenshot('provider-only-independent-test-root-selected-before-allow')
    click('[role="button"]:text-is("Allow access"):visible', 'Allow access')

