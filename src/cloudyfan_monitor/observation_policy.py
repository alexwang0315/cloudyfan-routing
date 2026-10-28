"""Operator adoption of known local observation rules; never execution policy."""
from cloudyfan_jev.task_features import POLICIES
from cloudyfan_jev import task_recommendations as advice
from types import MappingProxyType

from .store import MonitorError, digest


VERSIONS = {'features-v1': 'local-deterministic-v1', 'features-v2': 'local-deterministic-v2',
            'features-v2-advice-v1': 'local-deterministic-v2'}


class ObservationPolicy:
    def __init__(self, gateway, *, binding_port=None):
        from .integration import StoreFeatureBindingPort
        self.gateway = gateway
        service = gateway.service
        port = binding_port if binding_port is not None else StoreFeatureBindingPort(gateway.store)
        current = service.creation_features
        self.prepared = MappingProxyType({version: service.prepare_creation_observation({
            'policy_bundle': dict(POLICIES[feature]), 'binding_port': port,
            **({'compute_clock': current.compute_clock} if current is not None else {}),
        }, recommendations=advice.enabled_policy() if version == 'features-v2-advice-v1' else None)
            for version, feature in VERSIONS.items()})
        self._binding_options = MappingProxyType({version: MappingProxyType(value)
                                                  for version, value in self._prepare_versions().items()})
        # Legacy App assembly may not install a reader. Publishing with capture
        # disabled still reconnects the original Store without recapturing data.
        service.publish_creation_observation(current if current is not None else self.prepared['features-v1'],
                                             capture=current is not None)
        gateway.observation_policy = self
        try:
            state = gateway.store.observation_control()
            if state is not None:
                self._apply(state)
        except MonitorError:
            # Optional observation failure cannot prevent an original legal
            # task. Bind checks the durable selection in its own transaction.
            pass

    def _fingerprint(self, version):
        if version is None:
            return digest(None)
        prepared = self.prepared[version]
        return digest({'features': dict(prepared.policy) if prepared.policy is not None else None,
                       'registered': dict(prepared.registered),
                       'recommendations': {name: dict(value)
                                           for name, value in prepared.recommendation_adoptions.items()}})

    def _prepare_versions(self):
        return {None: {'fingerprint': self._fingerprint(None), 'policy': None, 'contracts': None,
                       'recommendations': None, 'capture_absent': True},
                **{version: {'fingerprint': self._fingerprint(version), 'policy': recorder.policy,
                             'contracts': recorder.registered, 'recommendations': recorder.recommendation_adoptions,
                             'capture_absent': False} for version, recorder in self.prepared.items()}}

    def binding_versions(self):
        return self._binding_options

    def _validate(self, state):
        if (type(state.get('revision')) is not int or state['revision'] < 1
                or state.get('version') is not None and state['version'] not in self.prepared
                or state.get('fingerprint') != self._binding_options[state.get('version')]['fingerprint']):
            raise MonitorError('observation_policy_unavailable', 503)

    def _apply(self, state):
        self._validate(state)
        version = state['version']
        self.gateway.service.publish_creation_observation(self.prepared[version] if version is not None else None)

    def snapshot(self):
        state = self.gateway.store.observation_control()
        if state is not None:
            self._validate(state)
        recorder = (self.prepared.get(state['version']) if state is not None
                    else self.gateway.service.creation_features)
        return {'schema_version': 'creation-observation-control-v1',
                'revision': state['revision'] if state is not None else 0,
                'active_version': state['version'] if state is not None else None,
                'configuration_source': 'operator' if state is not None else 'host',
                'capture_enabled': recorder is not None,
                'recommendations_enabled': bool(recorder is not None and recorder.recommendation_adoptions),
                'available_versions': [{'version': version, 'feature_version': feature,
                                        'recommendation_rule_version': advice.RULE_VERSION
                                        if version == 'features-v2-advice-v1' else None}
                                       for version, feature in VERSIONS.items()],
                'scope': 'New task observations only; existing tasks retain their original records.'}

    def activate(self, revision, version):
        if type(revision) is not int or revision < 0:
            raise MonitorError('invalid_observation_revision')
        if version is not None and (type(version) is not str or version not in VERSIONS):
            raise MonitorError('observation_rule_not_registered', 409)
        state = self.gateway.store.change_observation_control(revision, version, self._binding_options[version]['fingerprint'])
        self._apply(state)
        return self.snapshot()
