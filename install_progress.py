"""Bounded, secret-free in-process progress for protocol installation.

This registry is advisory, not a replacement for the peer lifecycle fence or
the persisted server configuration. Missing entries always mean unknown.
"""
from collections import OrderedDict
from threading import RLock
from time import monotonic
from uuid import UUID


PHASES = frozenset({'queued', 'connecting', 'validating', 'docker',
                    'installing', 'committing', 'linking'})
FINAL_STATES = frozenset({'succeeded', 'validation_failed', 'uncertain',
                          'conflict', 'blocked'})


class InstallProgress:
    def __init__(self, maximum=128):
        if maximum < 1:
            raise ValueError('maximum must be positive')
        self._maximum = maximum
        self._entries = OrderedDict()
        self._active = None
        self._lock = RLock()

    @staticmethod
    def _id(value):
        return str(UUID(str(value)))

    def begin(self, operation_id, server_id, protocol):
        """Return accepted, replay, conflict, busy or full; never run work."""
        operation_id = self._id(operation_id)
        if type(server_id) is not int or server_id < 0:
            raise ValueError('invalid server')
        if not isinstance(protocol, str) or not protocol or len(protocol) > 64:
            raise ValueError('invalid protocol')
        with self._lock:
            previous = self._entries.get(operation_id)
            if previous:
                return 'replay' if (previous['server_id'], previous['protocol']) == (server_id, protocol) else 'conflict'
            if self._active is not None:
                return 'busy'
            while len(self._entries) >= self._maximum:
                self._entries.popitem(last=False)
            self._entries[operation_id] = {
                'operation_id': operation_id, 'server_id': server_id,
                'protocol': protocol, 'state': 'running', 'phase': 'queued',
                'started': monotonic(), 'protocol_saved': None,
            }
            self._active = operation_id
            return 'accepted'

    def phase(self, operation_id, phase):
        if phase not in PHASES:
            raise ValueError('invalid phase')
        operation_id = self._id(operation_id)
        with self._lock:
            entry = self._entries.get(operation_id)
            if not entry or entry['state'] != 'running':
                return False
            entry['phase'] = phase
            return True

    def finish(self, operation_id, state, *, protocol_saved=None):
        if state not in FINAL_STATES:
            raise ValueError('invalid terminal state')
        if protocol_saved is not None and type(protocol_saved) is not bool:
            raise ValueError('invalid persisted state')
        if state == 'succeeded' and protocol_saved is not True:
            raise ValueError('success requires confirmed persistence')
        operation_id = self._id(operation_id)
        with self._lock:
            entry = self._entries.get(operation_id)
            if not entry or entry['state'] != 'running':
                return False
            entry['state'] = state
            entry['protocol_saved'] = protocol_saved
            if self._active == operation_id:
                self._active = None
            return True

    def get(self, operation_id, server_id):
        operation_id = self._id(operation_id)
        with self._lock:
            entry = self._entries.get(operation_id)
            if not entry or entry['server_id'] != server_id:
                return {'state': 'unknown'}
            return {key: entry[key] for key in ('operation_id', 'server_id',
                    'protocol', 'state', 'phase', 'protocol_saved')} | {
                        'elapsed_seconds': max(0, int(monotonic() - entry['started']))}
