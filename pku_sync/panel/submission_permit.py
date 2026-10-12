"""Non-expiring account-service fence before formal teaching-portal POSTs."""
from urllib.parse import quote, urlencode

from .. import catalog_snapshots as catalog
from .assignment_workflow import PreparationError, _requirements, _verified_saved_receipt


class SubmissionPermit:
    def __init__(self, settings, metadata, assignment, save):
        self.settings, self.metadata, self.assignment, self.save = settings, metadata, assignment, save
        self.armed = False
        self.recovered_receipt = None
        self.suffix = '/assignments/' + quote(metadata['content_id'], safe='') + '/submission'

    def _call(self, action='', extra=None):
        identity = self.metadata['submission_fence']
        body = {'revision': identity['revision'], 'file_sha256': self.metadata['sha256'],
                'portal_account_sha256': self.metadata['portal_account_sha256'],
                'operation_id': self.metadata['draft_id']}
        if identity.get('permit_id'):
            body['permit_id'] = identity['permit_id']
        body.update(extra or {})
        try:
            return catalog._call(self.settings, 'POST', self.metadata['course_id'], self.suffix + action, body)
        except catalog.CatalogUnavailable as exc:
            raise PreparationError('账号服务未确认提交许可；请求可能仍在等待核验，禁止重复发送。') from exc

    def _validate(self, result, state=None):
        if (result.get('operation_id') != self.metadata['draft_id']
                or result.get('revision') != self.metadata['submission_fence']['revision']
                or result.get('file_sha256') != self.metadata['sha256']
                or not isinstance(result.get('permit_id'), str) or not result['permit_id']
                or (state is not None and result.get('state') != state)):
            raise PreparationError('服务端提交许可与这份草稿不一致，禁止发送。')
        saved = self.metadata['submission_fence'].get('permit_id')
        if saved and saved != result['permit_id']:
            raise PreparationError('服务端提交许可身份已变化，禁止发送。')

    def _read(self):
        suffix = (self.suffix + '/' + quote(self.metadata['draft_id'], safe='') + '?' +
                  urlencode({'portal_account_sha256': self.metadata['portal_account_sha256']}))
        try:
            return catalog._call(self.settings, 'GET', self.metadata['course_id'], suffix)
        except catalog.CatalogUnavailable as exc:
            raise PreparationError('账号服务中的原提交记录尚未确认，已保留草稿；禁止重复发送。') from exc

    def reconcile(self):
        """Recover receipt bookkeeping or retire a provably unarmed request."""
        identity = self.metadata.get('submission_fence')
        if not isinstance(identity, dict) or type(identity.get('protocol')) is not int or identity['protocol'] != 1:
            raise PreparationError('旧提交记录没有可验证的共享许可，请到教学网核对；禁止自动解除。')
        result = self._read()
        self._validate(result)
        identity.update(permit_id=result['permit_id'], state=result['state'])
        self.save()
        if result['state'] in ('verification_pending', 'submitted'):
            if (self.metadata.get('state') not in ('receipt_verified', 'submitted')
                    or not _verified_saved_receipt({**self.metadata, 'state': 'submitted'})):
                raise PreparationError('提交可能已发出，但没有本次完整回执；请到教学网核对，其他设备也禁止重发。')
            self.complete(self.metadata['receipt'])
            self.metadata['state'] = 'submitted'
            self.save()
            return {'state': 'submitted', 'message': '已恢复回执保存，未重新发送作业。'}
        if result['state'] == 'acquired':
            if self._call('/release') != {'released': True}:
                raise PreparationError('未发送许可的释放未确认，已保留记录。')
            result = self._read()
            self._validate(result, 'aborted')
        elif result['state'] != 'aborted':
            raise PreparationError('提交记录状态未确认，禁止自动解除。')
        identity['state'] = 'aborted'
        self.metadata['state'] = 'aborted'
        self.save()
        return {'state': 'aborted', 'message': '账号服务已确认该请求未进入发送阶段，可重新准备并预览新草稿。'}

    def __enter__(self):
        if not self.metadata.get('submission_fence'):
            try:
                current = catalog.current(self.settings, self.metadata['course_id'])
            except catalog.CatalogUnavailable as exc:
                raise PreparationError('账号级正式作业快照不可用，请先更新目录；未发送作业。') from exc
            matches = [item for item in current['snapshot']['sections']['assignments']['items']
                       if item.get('content_id') == self.metadata['content_id'] and item.get('source') != 'calendar']
            if len(matches) != 1 or _requirements(matches[0]) != self.metadata['requirements_sha256']:
                raise PreparationError('服务端最新正式作业要求与预览不一致，请更新目录并重新准备。')
            self.metadata['submission_fence'] = {'protocol': 1, 'revision': current['revision']}
            self.save()
        result = self._call()
        self._validate(result)
        self.metadata['submission_fence'].update(permit_id=result['permit_id'], state=result['state'])
        self.save()
        if result['state'] in ('verification_pending', 'submitted'):
            self.armed = True
            if (self.metadata.get('state') not in ('receipt_verified', 'submitted')
                    or not _verified_saved_receipt({**self.metadata, 'state': 'submitted'})):
                raise PreparationError('该请求已发出或尚待核验；没有本次完整回执，禁止再次提交。')
            receipt = self.metadata['receipt']
            self.complete(receipt)
            self.recovered_receipt = receipt
        elif result['state'] != 'acquired':
            raise PreparationError('这份草稿的提交许可已结束，请重新准备。')
        return self

    def arm(self):
        # A lost arm response is ambiguous. Do not cancel, expire or POST.
        self.armed = True
        result = self._call('/arm')
        self._validate(result, 'verification_pending')
        verified = self._read()
        self._validate(verified, 'verification_pending')
        self.metadata['submission_fence']['state'] = 'verification_pending'
        self.save()

    def complete(self, receipt):
        if not _verified_saved_receipt({**self.metadata, 'state': 'submitted', 'receipt': receipt}):
            raise PreparationError('本次教学网回执内容或目标未验证，保留待核验状态。')
        fields = ('course_id', 'content_id', 'portal_verified', 'verified_at', 'review_history_sha256')
        proof = {key: receipt[key] for key in fields}
        result = self._call('/complete', {'receipt': proof})
        self._validate(result, 'submitted')
        verified = self._read()
        self._validate(verified, 'submitted')
        if verified.get('receipt') != proof:
            raise PreparationError('账号服务中的回执摘要未验证，保留本地回执供恢复。')
        self.metadata['submission_fence']['state'] = 'submitted'
        self.save()

    def __exit__(self, *_):
        if not self.armed:
            if self._call('/release') != {'released': True}:
                raise PreparationError('未发送许可的释放未确认，请核对账号服务。')
            self.metadata['submission_fence']['state'] = 'aborted'
            self.save()
