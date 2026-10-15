"""Strict independent account/OSS evidence, never infer cleanup from an empty list."""
import math
import re


def verified_audio_cleanup(before, after, source_id, operation_ids):
    """Bind newly charged ASR jobs to created/deleted objects and fresh real HEAD.

    Both snapshots must come from authenticated /v1/operations GETs, whose
    deployed implementation performs actual account-owned OSS HEAD requests.
    This predicate cannot turn absent historic instrumentation into evidence.
    """
    try:
        if (not isinstance(source_id, str) or not source_id.startswith('pku:recording:')
                or not isinstance(operation_ids, (set, list, tuple)) or not operation_ids):
            return False
        wanted = set(operation_ids)
        if len(wanted) != len(operation_ids) or any(not isinstance(op, str)
                or not re.fullmatch(r'asr:[A-Za-z0-9_-]+', op) for op in wanted):
            return False
        for snapshot in (before, after):
            if (snapshot.get('schema') != 1 or type(snapshot.get('schema')) is not int
                    or snapshot.get('complete') is not True
                    or snapshot.get('source') != 'account_cumulative_usage_rows'
                    or any(not isinstance(snapshot.get(key), list)
                           for key in ('jobs', 'settlements', 'oss_objects', 'oss_history'))):
                return False
            stamp = snapshot.get('observed_at')
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0:
                return False
        if after['observed_at'] < before['observed_at']:
            return False
        if any(row.get('operation_id') in wanted for row in before['jobs'] + before['settlements']):
            return False
        groups = {}
        seen_events = set()
        old_ids = {row['id'] for row in before['oss_history']}
        for row in after['oss_history']:
            if row.get('operation_id') not in wanted:
                continue
            if row.get('source_id') != source_id:
                return False
            event_id, stamp, object_id = row.get('id'), row.get('observed_at'), row.get('object_key_sha256')
            if (type(event_id) is not int or event_id <= 0 or event_id in seen_events or event_id in old_ids
                    or type(stamp) not in (int, float) or not math.isfinite(stamp)
                    or not before['observed_at'] <= stamp <= after['observed_at']
                    or not isinstance(object_id, str) or not re.fullmatch(r'[a-f0-9]{64}', object_id)):
                return False
            seen_events.add(event_id)
            groups.setdefault((row['operation_id'], object_id), []).append(row)
        if {op for op, _ in groups} != wanted:
            return False
        for op in wanted:
            jobs = [row for row in after['jobs'] if row.get('operation_id') == op]
            charges = [row for row in after['settlements'] if row.get('operation_id') == op]
            if (len(jobs) != 1 or jobs[0].get('status') != 'done' or len(charges) != 1
                    or type(charges[0].get('charged_seconds')) is not int or charges[0]['charged_seconds'] <= 0
                    or jobs[0].get('seconds_charged') != charges[0]['charged_seconds']):
                return False
        for (op, object_id), events in groups.items():
            if [row['id'] for row in events] != sorted(row['id'] for row in events):
                return False
            created = [row for row in events if row.get('event') == 'created']
            latest = events[-1]
            head = latest.get('current_head')
            if (len(created) != 1 or created[0].get('observed_exists') != 1
                    or type(created[0].get('observed_exists')) is not int
                    or type(created[0].get('bytes')) is not int or created[0]['bytes'] <= 0
                    or latest.get('event') != 'deleted' or latest.get('observed_exists') != 0
                    or type(latest.get('observed_exists')) is not int
                    or latest['id'] <= created[0]['id'] or latest['observed_at'] < created[0]['observed_at']
                    or not isinstance(head, dict) or head.get('exists') is not False
                    or type(head.get('status')) is not int or head['status'] != 404
                    or head.get('bytes') is not None):
                return False
            if any(row.get('id') == object_id or row.get('operation_id') == op
                   for row in after['oss_objects']):
                return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
