"""Observe actual identity actions; no callback, account or installer synthesis."""
from contextlib import contextmanager
from pathlib import Path
import re

from .core import ResourceBlocked, sha256


@contextmanager
def inject(run, variant):
    if run.mode != 'fault' or run.context.get('target') != 'windows':
        raise ResourceBlocked('actual_identity_behavior_requires_windows_fault_run')
    start, network_start = len(run.actions), len(run.network)
    receipt = {'variant':variant, 'boundary':'actual-owned-native-identity-behavior', 'observed':False}
    yield receipt
    actions, network = run.actions[start:], run.network[network_start:]
    if variant == 'oauth-cancel':
        callbacks = [r['actual_provider_oauth_callback'] for r in actions if 'actual_provider_oauth_callback' in r]
        ok = (len(callbacks) == 1 and callbacks[0].get('passed') is True
              and callbacks[0].get('accepted') is False and callbacks[0].get('callback_count') == 1
              and callbacks[0].get('callback_fabricated') is False
              and re.fullmatch('[0-9a-f]{64}', callbacks[0].get('state_sha256', '')) is not None
              and sum(r.get('host') == 'localhost' and r.get('path') == '/callback'
                      and r.get('method') == 'GET' for r in network) >= 1
              and not any(r.get('method') == 'POST' and r.get('path') == '/v1/notion/exchange' for r in network))
    elif variant == 'switch-account':
        switches = [r['actual_native_account_switch'] for r in actions if 'actual_native_account_switch' in r]
        identities = [r['actual_current_account_identity'] for r in actions if 'actual_current_account_identity' in r]
        logins = [r for r in network if r.get('boundary') == 'account'
                  and r.get('path') == '/v1/auth/login' and r.get('method') == 'POST']
        ok = (len(switches) == 1 and switches[0].get('passed') is True
              and len(identities) == 2 and identities[0]['email_sha256'] != identities[1]['email_sha256']
              and identities[0]['token_sha256'] != identities[1]['token_sha256']
              and len(logins) == 2 and all(r.get('upstream_status') == r.get('status') == 200
                                          and r.get('fault') is None for r in logins))
    elif variant == 'upgrade-restart':
        upgrades = [r['actual_os_upgrade'] for r in actions if 'actual_os_upgrade' in r]
        outcomes = [r['actual_native_upgrade_restart'] for r in actions if 'actual_native_upgrade_restart' in r]
        ok = len(upgrades) == len(outcomes) == 1 and outcomes[0].get('passed') is True
        if ok:
            actual = upgrades[0]
            file = (run.case_dir / actual['receipt_file']).resolve()
            installed = Path(run.need('instances.primary.installed_path')).resolve()
            ok = (file.is_relative_to(run.case_dir.resolve()) and sha256(file) == actual['receipt_sha256']
                  and actual.get('artifact_sha256') == run.context['artifact_sha256']
                  and actual.get('installed_sha256') == sha256(installed)
                  and actual.get('generated_by') == 'os-installer'
                  and all(actual.get(k) is True for k in ('student_product_registry_values_restored',
                      'previous_install_pointers_suppressed_during_isolated_install',
                      'actual_nsis_manufacturer_key_preserved'))
                  and any(r.get('native_quit', {}).get('instance') == 'primary'
                          and r['native_quit'].get('graceful') is True for r in actions))
    else:
        raise ResourceBlocked('unsupported_identity_behavior')
    if not ok:
        raise ResourceBlocked('actual_identity_behavior_not_exercised:' + variant)
    receipt['observed'] = True
    run.actions.append({'actual_identity_behavior_boundary':dict(receipt)})
