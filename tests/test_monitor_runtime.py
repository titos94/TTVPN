import unittest

from isolated_monitor import MAX_AGE
from monitor_runtime import MAX_PRODUCERS, MonitorRuntime


def event(slot='slot-00', component='panel', timestamp=1000, state='ok'):
    return {'schema_version': 1, 'id': slot, 'component': component,
            'timestamp': timestamp, 'state': state, 'metrics': {'cpu_percent': 42}}


def runtime():
    return MonitorRuntime({'slot-00': 'panel', 'slot-01': 'panel',
                           'slot-02': 'proxy', 'slot-03': 'telegram',
                           'slot-04': 'vpn_node'},
                          {'producer-a': {'slot-00', 'slot-01'},
                           'producer-b': {'slot-02', 'slot-03', 'slot-04'}})


class RuntimeTests(unittest.TestCase):
    def test_required_multislot_staleness_and_status_only(self):
        r = runtime()
        for i, component in enumerate(('panel', 'panel', 'proxy', 'telegram', 'vpn_node')):
            if i != 1:
                r.accept('producer-a' if i < 2 else 'producer-b',
                         event(f'slot-{i:02}', component), now=1000)
        result = r.snapshot(now=1000)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(next(x['state'] for x in result['slots'] if x['id'] == 'slot-01'), 'unknown')
        r.accept('producer-a', event('slot-01'), now=1000)
        self.assertEqual(r.snapshot(now=1000)['state'], 'ok')
        self.assertEqual(r.snapshot(now=1000 + MAX_AGE)['state'], 'ok')
        self.assertEqual(r.snapshot(now=1000 + MAX_AGE + 1)['state'], 'unknown')
        self.assertEqual(r.snapshot(now=999)['state'], 'unknown')
        self.assertNotIn('metrics', repr(r.snapshot(now=1000)))

    def test_auth_component_order_and_no_state_change_on_rejection(self):
        r = runtime()
        r.accept('producer-a', event(), now=1000)
        before = r.snapshot(now=1000)
        attempts = [('producer-b', event()), ('unknown', event()),
                    ('producer-a', event(component='proxy', timestamp=1001)),
                    ('producer-a', event('slot-99', timestamp=1001)),
                    ('producer-a', event(timestamp=1000)),
                    ('producer-a', event(timestamp=999))]
        for identity, item in attempts:
            with self.subTest(identity=identity, item=item):
                with self.assertRaises(ValueError):
                    r.accept(identity, item, now=1001)
                self.assertEqual(r.snapshot(now=1000), before)
        r.accept('producer-a', event(timestamp=1001, state='down'), now=1001)
        self.assertEqual(r.snapshot(now=1001)['state'], 'down')

    def test_sensitive_extra_fields_labels_and_copy(self):
        r = runtime()
        for field in ('secret', 'token', 'key', 'payment', 'labels'):
            sample = event()
            sample[field] = 'secret-value'
            with self.assertRaises(ValueError) as error:
                r.accept('producer-a', sample, now=1000)
            self.assertNotIn('secret-value', str(error.exception))
            sample = event()
            sample['metrics'][field] = 1
            with self.assertRaises(ValueError):
                r.accept('producer-a', sample, now=1000)
        sample = event()
        r.accept('producer-a', sample, now=1000)
        sample['state'] = 'down'
        sample['metrics']['cpu_percent'] = 999
        result = r.snapshot(now=1000)
        self.assertEqual(result['slots'][0]['state'], 'ok')
        result['slots'][0]['state'] = 'down'
        self.assertEqual(r.snapshot(now=1000)['slots'][0]['state'], 'ok')

    def test_configuration_bounds_and_copy(self):
        slots = {'slot-00': 'panel'}
        producers = {'a': {'slot-00'}}
        r = MonitorRuntime(slots, producers)
        slots['slot-01'] = 'proxy'
        producers['a'].add('slot-01')
        with self.assertRaises(ValueError):
            r.accept('a', event('slot-01', 'proxy'), now=1000)
        invalid = [({}, producers), ({'slot-100': 'panel'}, {'a': {'slot-100'}}),
                   ({'slot-00': 'client'}, {'a': {'slot-00'}}),
                   ({'slot-00': 'panel'}, {'a': {'slot-01'}}),
                   ({'slot-00': 'panel'}, {'a': set()}),
                   ({'slot-00': 'panel'}, {str(i): {'slot-00'} for i in range(MAX_PRODUCERS + 1)}),
                   ({f'slot-{i:03}': 'panel' for i in range(101)}, {'a': {'slot-000'}})]
        for bad_slots, bad_producers in invalid:
            with self.assertRaises(ValueError):
                MonitorRuntime(bad_slots, bad_producers)
        with self.assertRaises(ValueError):
            r.snapshot(now=True)

    def test_future_event_cannot_poison_slot(self):
        r = runtime()
        with self.assertRaises(ValueError):
            r.accept('producer-a', event(timestamp=2000), now=1000)
        r.accept('producer-a', event(timestamp=1000), now=1000)
        self.assertEqual(r.snapshot(now=1000)['slots'][0]['state'], 'ok')

    def test_stale_event_and_untrusted_now_are_rejected(self):
        r = runtime()
        for timestamp, now in ((1, 1000), (1000, True), (1000, float('nan'))):
            with self.assertRaises(ValueError):
                r.accept('producer-a', event(timestamp=timestamp), now=now)


if __name__ == '__main__':
    unittest.main()
