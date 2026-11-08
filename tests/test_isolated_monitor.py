import copy
import itertools
import math
import unittest

from isolated_monitor import COMPONENTS, MAX_AGE, MAX_EVENTS, MAX_TIMESTAMP, aggregate, validate_event


def event(component='panel', identifier='slot-00', timestamp=1000, state='ok'):
    return {'schema_version': 1, 'component': component, 'id': identifier,
            'timestamp': timestamp, 'state': state,
            'metrics': {'cpu_percent': 20, 'memory_percent': 30.5}}


def complete():
    return [event(component=c) for c in sorted(COMPONENTS)]


class ValidationTests(unittest.TestCase):
    def test_rejects_extra_sensitive_fields_and_metric_names(self):
        for field in ('log', 'url', 'ip', 'client_name', 'key', 'payment'):
            with self.subTest(field=field):
                sample = event()
                sample[field] = 'secret'
                with self.assertRaises(ValueError) as error:
                    validate_event(sample)
                self.assertNotIn('secret', str(error.exception))
                sample = event()
                sample['metrics'][field] = 1
                with self.assertRaises(ValueError):
                    validate_event(sample)

    def test_fields_types_and_ids(self):
        for field, values in {
            'schema_version': [True, 1.0, 2, None],
            'component': ['client', 'Panel', [], None],
            'id': ['user', 'slot-1', 'slot-001', 'slot-00\n', '192.168.0.1', None],
            'state': ['OK', 'healthy', [], None],
            'timestamp': [-1, MAX_TIMESTAMP + 1, float('nan'), float('inf'), True, '1000', 10**1000],
            'metrics': [[], None, {'cpu_percent': '5'}, {'cpu_percent': True}],
        }.items():
            for value in values:
                with self.subTest(field=field, value=str(value)[:30]):
                    sample = event()
                    sample[field] = value
                    with self.assertRaises(ValueError):
                        validate_event(sample)
        for value in [None, [], 'event', 1, True, {}, {'schema_version': 1}]:
            with self.assertRaises(ValueError):
                validate_event(value)

    def test_metric_bounds(self):
        for name in ('cpu_percent', 'memory_percent', 'disk_percent'):
            for value in (0, 100, 0.1):
                sample = event()
                sample['metrics'] = {name: value}
                self.assertEqual(validate_event(sample)['metrics'][name], value)
            for value in (-0.1, 100.1, math.nan, math.inf, -math.inf, [], None):
                sample = event()
                sample['metrics'] = {name: value}
                with self.assertRaises(ValueError):
                    validate_event(sample)

    def test_independent_copy(self):
        original = event()
        cloned = validate_event(original)
        cloned['metrics']['cpu_percent'] = 99
        self.assertEqual(original['metrics']['cpu_percent'], 20)


class AggregateTests(unittest.TestCase):
    def test_coverage_and_priority(self):
        self.assertEqual(aggregate([], now=1000)['state'], 'unknown')
        self.assertEqual(aggregate(complete(), now=1000)['state'], 'ok')
        for missing in COMPONENTS:
            items = [e for e in complete() if e['component'] != missing]
            result = aggregate(items, now=1000)
            self.assertEqual(result['state'], 'unknown')
            self.assertEqual(result['missing_components'], [missing])
        for state in ('unknown', 'degraded', 'down'):
            items = complete()
            items[0]['state'] = state
            self.assertEqual(aggregate(items, now=1000)['state'], state)
        items = [event(state='degraded'), event('proxy', state='down')]
        self.assertEqual(aggregate(items, now=1000)['state'], 'down')

    def test_stale_future_and_boundary_without_mutation(self):
        for timestamp, expected in [(1000 - MAX_AGE, 'ok'),
                                    (1000 - MAX_AGE - 0.1, 'unknown'),
                                    (1000.1, 'unknown')]:
            items = complete()
            items[0]['timestamp'] = timestamp
            original = copy.deepcopy(items)
            result = aggregate(items, now=1000)
            self.assertEqual(result['state'], expected)
            self.assertEqual(items, original)
            if expected == 'unknown':
                self.assertEqual(result['events'][0]['metrics'], {})

    def test_order_duplicate_and_no_shared_memory(self):
        items = complete()
        expected = aggregate(items, now=1000)
        for permutation in itertools.permutations(items):
            self.assertEqual(aggregate(list(permutation), now=1000), expected)
        with self.assertRaises(ValueError):
            aggregate([event(), event(timestamp=999)], now=1000)
        expected['events'][0]['metrics'].clear()
        self.assertTrue(items[0]['metrics'])
        self.assertEqual(aggregate([], now=1000)['events'], [])

    def test_capacity_and_input_types(self):
        items = [event(component, f'slot-{i:02d}')
                 for component in sorted(COMPONENTS) for i in range(100)]
        self.assertEqual(len(aggregate(items[:MAX_EVENTS], now=1000)['events']), MAX_EVENTS)
        with self.assertRaises(ValueError):
            aggregate(items[:MAX_EVENTS + 1], now=1000)
        for bad in (None, (), {}, 'events', iter([]), True):
            with self.assertRaises(ValueError):
                aggregate(bad, now=1000)
        for bad in (None, True, '1000', math.nan, math.inf, -1, MAX_TIMESTAMP + 1):
            with self.assertRaises(ValueError):
                aggregate([], now=bad)


if __name__ == '__main__':
    unittest.main()
