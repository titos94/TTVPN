"""Backoff политики каскада: неудачное переключение не должно повторяться каждый такт.

Тесты чистые и детерминированные: время передаётся явно, сеть и SSH не нужны.
"""
import exit_failover as policy


def cfg(**overrides):
    base = policy.default_config()
    base.update({'enabled': True, 'failure_threshold': 1, 'backup_uids': ['exit-b']})
    base.update(overrides)
    return base


def state(**overrides):
    base = policy.default_state()
    base.update({
        'primary_uid': 'exit-a',
        'active_uid': 'exit-a',
        'last_check_at': policy.isoformat(1000.0),
    })
    base.update(overrides)
    return base


def test_first_switch_failure_sets_next_attempt_at():
    result = policy.record_switch_failure(
        state(), now=1000.0, interval_seconds=45)

    assert result['consecutive_switch_failures'] == 1
    assert result['last_switch_failure_at'] == policy.isoformat(1000.0)
    assert policy.parse_ts(result['next_attempt_at']) == 1060.0


def test_backoff_grows_and_is_capped():
    # 45 s interval → cap 180 s.
    assert [policy.switch_backoff_seconds(n, 45) for n in range(1, 6)] == [
        60, 120, 180, 180, 180,
    ]
    # A longer interval raises the cap, so growth is visible for longer.
    assert [policy.switch_backoff_seconds(n, 300) for n in range(1, 7)] == [
        60, 120, 240, 480, 960, 1200,
    ]
    # Junk input must not raise, and never returns less than the base delay.
    assert policy.switch_backoff_seconds('x', 'y') == 60
    assert policy.switch_backoff_seconds(3, 0) == 180


def test_cooldown_skips_the_next_tick():
    st = state(next_attempt_at=policy.isoformat(1060.0))

    decision = policy.decide(
        config=cfg(),
        state=st,
        now=1000.0,
        active_ok=False,
        active_reason=policy.REASON_HANDSHAKE_STALE,
        primary_ok=False,
        healthy_uids={'exit-b'},
    )

    assert decision['action'] == policy.ACTION_STAY
    assert decision['reason'] == policy.REASON_SWITCH_COOLDOWN
    assert decision['target_uid'] is None


def test_cooldown_does_not_count_as_a_new_failure():
    st = state(next_attempt_at=policy.isoformat(1060.0), consecutive_failures=7)

    decision = policy.decide(
        config=cfg(),
        state=st,
        now=1000.0,
        active_ok=False,
        active_reason=policy.REASON_HANDSHAKE_STALE,
        primary_ok=False,
        healthy_uids={'exit-b'},
    )

    assert decision['state']['consecutive_failures'] == 7
    assert decision['state']['consecutive_successes'] == 0
    assert decision['state']['consecutive_switch_failures'] == 0


def test_backoff_resets_after_a_successful_switch():
    st = policy.record_switch_failure(state(), now=1000.0, interval_seconds=45)

    switched = policy.record_switch(
        st, now=1200.0, from_uid='exit-a', to_uid='exit-b',
        action=policy.ACTION_FAILOVER, reason=policy.REASON_OK)

    assert switched['consecutive_switch_failures'] == 0
    assert switched['next_attempt_at'] == ''
    assert switched['active_uid'] == 'exit-b'


def test_state_without_backoff_fields_behaves_as_before():
    legacy = {
        'primary_uid': 'exit-a',
        'active_uid': 'exit-a',
        'last_check_at': policy.isoformat(1000.0),
        'consecutive_failures': 0,
    }

    decision = policy.decide(
        config=cfg(),
        state=legacy,
        now=1000.0,
        active_ok=False,
        active_reason=policy.REASON_HANDSHAKE_STALE,
        primary_ok=False,
        healthy_uids={'exit-b'},
    )

    assert decision['action'] == policy.ACTION_FAILOVER
    assert decision['target_uid'] == 'exit-b'
    assert decision['state']['next_attempt_at'] == ''
    assert decision['state']['consecutive_switch_failures'] == 0


def test_routing_leak_reason_exists_and_is_distinct():
    assert policy.REASON_ROUTING_LEAK == 'routing_leak'
    assert policy.REASON_ROUTING_LEAK != policy.REASON_EGRESS_LEAK
    assert policy.REASON_ROUTING_LEAK != policy.REASON_SWITCH_COOLDOWN
