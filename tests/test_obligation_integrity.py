"""Missing declarations and unresolved symbols cannot become empty effects."""
import unittest
from copy import deepcopy
from infrastructure.stages.database.obligations.records import normalize_inference, check_symbols
from infrastructure.stages.frontend.triggers.design import validate_coverage
from infrastructure.stages.backend.primitives import ObligationCoverageError, apply_decomposition
from infrastructure.shared.agent.proposals import response_feedback
from test_frontend_flow import frontend_fixture


class ObligationIntegrity(unittest.TestCase):
    def test_api_repair_names_only_changed_prior_dependencies_and_preserves_design(self):
        def effects(*writes):
            return {'read': [], 'write': list(writes)}
        before = {'apis': {'API-old': effects('Account.id'), 'API-unrelated': effects('Other.id')},
            'requirements': {'REQ-1': ['API-old']}}
        original = deepcopy(before)
        obligations = {'requirements': {'REQ-1': effects('Account.id'),
            'REQ-2': effects('Account.id', 'Account.email')}}
        rejected = {'apis': {'API-old': effects('Account.id', 'Account.email'),
            'API-unrelated': effects('Other.name')}, 'requirement_apis': ['API-old']}
        with self.assertRaises(ObligationCoverageError) as caught:
            apply_decomposition(before, rejected, 'REQ-2', obligations)
        feedback = response_feedback(caught.exception, rejected)
        self.assertNotIn('Account.email', feedback['error'])
        details = feedback['details']
        self.assertEqual(details['excess']['write'], ['Account.email'])
        regression = details['accepted_regression']
        self.assertEqual(regression['affected_requirement'], 'REQ-1')
        self.assertEqual(regression['proposed_for'], 'REQ-2')
        self.assertEqual(regression['changed_effects'], {'API-old': {
            'added': effects('Account.email'), 'removed': effects()}})
        self.assertEqual(before, original)
        fixed = apply_decomposition(before, {'apis': {'API-new': effects('Account.id', 'Account.email')},
            'requirement_apis': ['API-new']}, 'REQ-2', obligations)
        self.assertEqual(fixed['apis']['API-old'], original['apis']['API-old'])

    def test_omitted_step_or_mode_differs_from_explicit_empty(self):
        value = {'requirement': {'read': [], 'write': []}, 'steps': {}}
        with self.assertRaises(KeyError):
            normalize_inference(value, ['S'])
        value['steps']['S'] = {'read': []}
        with self.assertRaises(KeyError):
            normalize_inference(value, ['S'])
        value['steps']['S']['write'] = []
        self.assertEqual(normalize_inference(value, ['S']), value)

    def test_unknown_field_is_rejected_even_if_not_related(self):
        schema = {'entities': {'Account': {'fields': {'id': {}}}}}
        check_symbols([{'read': ['Account.id'], 'write': []}], schema)
        with self.assertRaisesRegex(ValueError, 'Session.id'):
            check_symbols([{'read': ['Session.id'], 'write': []}], schema)

    def test_missing_frontend_call_or_step_is_rejected(self):
        _, tree, _, design, backend, obligations = frontend_fixture()
        obligations['steps'] = {sid: obligations['requirements'][rid]
            for rid in tree.leaves for sid in tree.own_steps[rid]}
        validate_coverage(design, backend, obligations, tree, tree.leaves)
        missing_call = deepcopy(design)
        missing_call['triggers']['TR-SAVE']['action'] = []
        with self.assertRaises(ObligationCoverageError):
            validate_coverage(missing_call, backend, obligations, tree, ['REQ-A'])
        missing_step = deepcopy(design)
        del missing_step['steps']['STEP-REQ-A']
        with self.assertRaises(KeyError):
            validate_coverage(missing_step, backend, obligations, tree, ['REQ-A'])
        # API possible effects include success writes, while rejection does not write.
        obligations['steps']['STEP-REQ-A'] = {'read': [], 'write': []}
        validate_coverage(design, backend, obligations, tree, ['REQ-A'])
