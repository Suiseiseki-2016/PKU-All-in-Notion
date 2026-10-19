"""Guarded recovery of real historical observations, never upload authorization.

No public write route exists. Operators must verify the exact original HTTP
body and hold their own transaction. Unknown task metadata stays unknown.
"""
from datetime import datetime
import hashlib
import json
import re
import time

PROJECTION_FIELDS={'id','operation_id','status','fingerprint','seconds_charged','reused'}
ARCHIVE_FIELDS=('id','account_id','state','fingerprint','seconds_charged','reused',
    'original_body_sha256','original_body_bytes','observed_at')

def validate_projection(row):
    if (not isinstance(row,dict) or set(row)!=PROJECTION_FIELDS
            or not isinstance(row['id'],str) or not re.fullmatch('[0-9a-f]{32}',row['id'])
            or row['operation_id']!='asr:'+row['id'] or row['status']!='done'
            or not isinstance(row['fingerprint'],str) or not re.fullmatch('[0-9a-f]{64}',row['fingerprint'])
            or type(row['seconds_charged']) is not int or row['seconds_charged']<0
            or type(row['reused']) is not int or row['reused'] not in (0,1)):
        raise ValueError('invalid original ASR task projection')

def selected_original_tasks(record, operation_ids, expected_body_sha256):
    if (not isinstance(record,dict) or record.get('service')!='account'
            or record.get('host')!='pku.aeoluswu.info' or record.get('path')!='/v1/operations'
            or record.get('method')!='GET' or type(record.get('status')) is not int or record['status']!=200
            or not isinstance(expected_body_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_body_sha256)
            or record.get('body_sha256')!=expected_body_sha256):
        raise ValueError('original response source or digest mismatch')
    body=record.get('readback')
    if (not isinstance(body,dict) or type(body.get('schema')) is not int or body['schema']!=1
            or body.get('source')!='account_cumulative_usage_rows' or body.get('complete') is not True):
        raise ValueError('original response contract mismatch')
    encoded=json.dumps(body,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf-8')
    if (not 0<len(encoded)<=4*1024*1024 or type(record.get('body_bytes')) is not int
            or len(encoded)!=record['body_bytes'] or hashlib.sha256(encoded).hexdigest()!=expected_body_sha256):
        raise ValueError('original response body mismatch')
    observed_at=record.get('observed_at')
    try:
        observed=datetime.fromisoformat(observed_at)
        if observed.tzinfo is None: raise ValueError('timezone absent')
    except (TypeError,ValueError):
        raise ValueError('original response observation time invalid') from None
    if (not isinstance(operation_ids,list) or not 0<len(operation_ids)<=100
            or len(set(operation_ids))!=len(operation_ids)
            or any(not isinstance(op,str) or not re.fullmatch('asr:[0-9a-f]{32}',op) for op in operation_ids)):
        raise ValueError('invalid selected original operation identities')
    jobs,settlements=body.get('jobs'),body.get('settlements')
    if not isinstance(jobs,list) or not isinstance(settlements,list):
        raise ValueError('original response jobs or settlements invalid')
    selected=[]
    for op in operation_ids:
        matches=[row for row in jobs if isinstance(row,dict) and row.get('operation_id')==op]
        charges=[row for row in settlements if isinstance(row,dict) and row.get('operation_id')==op]
        if len(matches)!=1 or len(charges)!=1:
            raise ValueError('original task or settlement is not unique')
        task=matches[0]
        validate_projection(task)
        charge=charges[0]
        if type(charge.get('charged_seconds')) is not int or charge['charged_seconds']!=task['seconds_charged']:
            raise ValueError('original task settlement mismatch')
        selected.append((task,charge))
    return selected

def restore_observed_asr_jobs(db,account_id,record,operation_ids,expected_body_sha256):
    if not db.in_transaction:
        raise ValueError('caller must hold a restoration transaction')
    if type(account_id) is not int or account_id<=0:
        raise ValueError('invalid current owner association')
    selected=selected_original_tasks(record,operation_ids,expected_body_sha256)
    pending=[]
    for task,charge in selected:
        rows=list(db.execute("SELECT id,seconds,recorded_at FROM usage WHERE account_id=? AND kind='transcribe' AND operation_id=?",
            (account_id,task['operation_id'])))
        if (len(rows)!=1 or rows[0]['seconds']!=task['seconds_charged']
                or charge.get('id')!='asr:'+str(rows[0]['id'])
                or charge.get('recorded_at')!=rows[0]['recorded_at']):
            raise ValueError('current owned original settlement mismatch')
        if any(db.execute('SELECT 1 FROM '+table+' WHERE id=?',(task['id'],)).fetchone()
                for table in ('direct_upload_grants','asr_job_receipts')):
            raise ValueError('original task already has a live grant or retained receipt')
        archive={'id':task['id'],'account_id':account_id,'state':task['status'],
            'fingerprint':task['fingerprint'],'seconds_charged':task['seconds_charged'],'reused':task['reused'],
            'original_body_sha256':expected_body_sha256,'original_body_bytes':record['body_bytes'],
            'observed_at':record['observed_at']}
        existing=db.execute('SELECT '+','.join(ARCHIVE_FIELDS)+' FROM asr_observed_jobs WHERE id=?',(task['id'],)).fetchone()
        if existing is not None:
            if dict(existing)!=archive:
                raise ValueError('immutable original ASR observation conflict')
        else:
            pending.append(archive)
    # Do not commit/rollback the caller's transaction. Savepoint makes even a
    # mid-batch DB failure atomic while preserving unrelated caller work.
    db.execute('SAVEPOINT asr_observation_restore')
    try:
        restored_at=time.time()
        for row in pending:
            db.execute('INSERT INTO asr_observed_jobs('+','.join(ARCHIVE_FIELDS)+',restored_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (*[row[key] for key in ARCHIVE_FIELDS],restored_at))
    except BaseException:
        db.execute('ROLLBACK TO asr_observation_restore')
        raise
    finally:
        db.execute('RELEASE asr_observation_restore')
    return len(pending)
