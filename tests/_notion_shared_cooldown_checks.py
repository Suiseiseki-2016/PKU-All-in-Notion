# Developer fake-clock checks copied exactly from the frozen red runner.
import threading
import types
import httpx
from pku_sync import notion

class Clock:

    def __init__(self, automatic=False):
        self.value = 0.0
        self.automatic = automatic
        self.changed = threading.Condition()
        self.conditions = []
        self.wait_entered = threading.Event()
        self.wait_count = 0
        self.sleep_values = []

    def monotonic(self):
        with self.changed:
            return self.value

    def advance(self, target):
        with self.changed:
            assert target >= self.value
            self.value = float(target)
            self.changed.notify_all()
        for condition in list(self.conditions):
            with condition:
                condition.notify_all()

    def sleep(self, seconds):
        assert seconds >= 0 and seconds < float('inf')
        self.sleep_values.append(seconds)
        target = self.monotonic() + seconds
        self.wait_entered.set()
        if self.automatic:
            self.advance(target)
            return
        with self.changed:
            assert self.changed.wait_for(lambda: self.value >= target, timeout=2), 'fixture wait did not terminate'

    def condition_class(self):
        clock = self

        class ClockCondition(threading.Condition):

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clock.conditions.append(self)

            def wait(self, timeout=None):
                assert timeout is not None and 0 < timeout < float('inf')
                clock.wait_count += 1
                assert clock.wait_count < 10000, 'product busywait/nonterminating fake-clock schedule'
                clock.wait_entered.set()
                if clock.automatic:
                    clock.advance(clock.monotonic() + timeout)
                    return True
                return super().wait(min(timeout, 0.05))
        return ClockCondition

def client(handler, token, direct=None):
    return notion.NotionClient(token, transport=httpx.MockTransport(handler), direct_transport=httpx.MockTransport(direct) if direct else None)

def install_clock(clock):
    notion.time = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    condition = clock.condition_class()
    notion.Condition = condition
    notion.threading = types.SimpleNamespace(Condition=condition, Lock=threading.Lock, RLock=threading.RLock)

def run_parallel(case_id, status, delay):
    clock = Clock()
    install_clock(clock)
    throttle = threading.Event()
    b_entered = threading.Event()
    starts = []
    errors = []
    token = 'developer-private-' + case_id
    calls_a = []

    def first(request):
        calls_a.append(clock.monotonic())
        if len(calls_a) == 1:
            throttle.set()
            return httpx.Response(status, headers={'Retry-After': str(delay)}, json={'code': 'rate_limited' if status == 429 else 'service_overload'})
        return httpx.Response(200, json={'object': 'user'})

    def second(request):
        starts.append(clock.monotonic())
        b_entered.set()
        return httpx.Response(200, json={'object': 'user'})

    def action(c):
        try:
            c.whoami()
        except BaseException as error:
            errors.append(type(error).__name__)
    a = client(first, token)
    b = client(second, token)
    worker_a = threading.Thread(target=action, args=(a,), daemon=True)
    worker_b = threading.Thread(target=action, args=(b,), daemon=True)
    worker_a.start()
    assert throttle.wait(1) and clock.wait_entered.wait(1)
    independent_starts = []
    with client(lambda request: independent_starts.append(clock.monotonic()) or httpx.Response(200, json={'object': 'user'}), token + '-other') as independent:
        independent.whoami()
    worker_b.start()
    b_entered.wait(0.2)
    before_deadline = list(starts)
    clock.advance(delay)
    worker_a.join(1)
    worker_b.join(1)
    assert not worker_a.is_alive() and (not worker_b.is_alive()) and (not errors)
    a.close()
    b.close()
    predicate = bool(starts) and all((value >= delay for value in starts)) and (not before_deadline)
    return {'case_id': case_id, 'response_status': status, 'retry_after': delay, 'shared_cooldown_predicate': predicate, 'same_token_other_client_transport_starts': starts, 'starts_observed_before_deadline_advance': before_deadline, 'throttled_request_transport_starts': calls_a, 'different_token_isolated_predicate': independent_starts == [0.0], 'all_owned_workers_terminated': True, 'developer_clock_only': True}

def run_terminal(case_id, direct=False, reopen=False):
    clock = Clock(automatic=True)
    install_clock(clock)
    token = 'developer-private-' + case_id
    calls = []
    direct_calls = []
    rejected = []
    delay = 61

    def handler(request):
        calls.append(clock.monotonic())
        if direct:
            raise httpx.ReadTimeout('developer read timeout', request=request)
        rejected.append(clock.monotonic())
        return httpx.Response(429, headers={'Retry-After': str(delay)}, json={'code': 'rate_limited'})

    def direct_handler(request):
        direct_calls.append(clock.monotonic())
        rejected.append(clock.monotonic())
        return httpx.Response(529, headers={'Retry-After': str(delay)}, json={'code': 'service_overload'})
    a = client(handler, token, direct_handler if direct else None)
    terminal_status = None
    try:
        a.whoami()
    except notion.NotionError as error:
        terminal_status = error.status
    assert terminal_status == (529 if direct else 429)
    assert len(calls) == 3 and len(direct_calls) == (1 if direct else 0)
    deadline = rejected[-1] + delay
    if reopen:
        a.close()
    starts = []
    with client(lambda request: starts.append(clock.monotonic()) or httpx.Response(200, json={'object': 'user'}), token) as b:
        b.whoami()
    if not reopen:
        a.close()
    return {'case_id': case_id, 'response_status': terminal_status, 'retry_after': delay, 'last_rejection_time': rejected[-1], 'shared_deadline': deadline, 'same_token_other_client_transport_starts': starts, 'shared_cooldown_predicate': bool(starts) and all((value >= deadline for value in starts)), 'original_attempt_budget_preserved': len(calls) == 3 and len(direct_calls) == (1 if direct else 0), 'closed_before_new_client': reopen, 'all_owned_workers_terminated': True, 'developer_clock_only': True}
