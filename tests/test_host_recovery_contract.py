"""Shared vectors exercise the central projection and independent host policy."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from bscli.core.host_contract import host_transport_recovery_strategy
from bscli.mcp.presentation import build_server_profile

ROOT = Path(__file__).parents[1]
REFERENCE_ROOT = str(ROOT / 'integrations' / 'reference-host')
if REFERENCE_ROOT not in sys.path:
    sys.path.insert(0, REFERENCE_ROOT)
from reference_host.interaction_driver import InteractionDriver
from reference_host.mcp_client import AgentBridgeMcpClient
from reference_host.recovery_policy import (
    interaction_resume_allowed, resume_claim_allowed,
    transport_recovery_strategy, transport_retry_allowed,
)

VECTORS = json.loads((ROOT / 'schemas/agent-host/v1/test-vectors.json').read_text())


class HostRecoveryContractTests(unittest.TestCase):
    def test_central_projection_and_independent_host_consume_shared_strategy_vectors(self):
        projected = build_server_profile(mcp_url='https://agentbridge.invalid/mcp')['hostContract']['transportRecovery']
        for vector in VECTORS['recoveryStrategies']:
            with self.subTest(vector=vector['name']):
                self.assertEqual(host_transport_recovery_strategy(vector['callClass']), vector['expected'])
                self.assertEqual(transport_recovery_strategy(vector['callClass']), vector['expected'])
                if vector['callClass'] in projected:
                    self.assertEqual(projected[vector['callClass']], vector['expected'])
        self.assertEqual(set(projected), {'read', 'prepare', 'commit', 'completedResume'})

    def test_independent_host_consumes_shared_budget_and_resume_vectors(self):
        for vector in VECTORS['retryDecisions']:
            value = vector['value']
            with self.subTest(vector=vector['name']):
                self.assertEqual(transport_retry_allowed(
                    retryable=value['retryable'], attempt=value['attempt'],
                    maximum_attempts=value['maximumAttempts'], canceled=value.get('canceled', False),
                    delay_ms=value.get('delayMs', 0), remaining_ms=value.get('remainingMs'),
                ), vector['expected'])
        for vector in VECTORS['interactionResumeDecisions']:
            with self.subTest(vector=vector['name']):
                self.assertEqual(interaction_resume_allowed(vector['value']), vector['expected'])
        for vector in VECTORS['resumeClaimDecisions']:
            with self.subTest(vector=vector['name']):
                self.assertEqual(resume_claim_allowed(
                    claimed=vector['value']['claimed'], task_terminal=vector['value']['taskTerminal'],
                ), vector['expected'])


class ReferenceRecoveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self, *, interaction, resume=None):
        task_driver = SimpleNamespace(
            get_interaction=AsyncMock(return_value=interaction),
            note_interaction_state=AsyncMock(),
            resume_interaction=resume or AsyncMock(),
            close_interaction=AsyncMock(),
            mark_poll_deadline=AsyncMock(),
            mark_interaction_driver_error=AsyncMock(),
        )
        return InteractionDriver(task_driver, poll_interval_seconds=.05), task_driver

    async def test_monitor_executes_shared_interaction_vectors(self):
        for vector in VECTORS['interactionResumeDecisions']:
            with self.subTest(vector=vector['name']):
                monitor, driver = self.monitor(interaction=vector['value'])
                driver.get_interaction.side_effect = [vector['value'], {'state': 'expired'}]
                with patch('reference_host.interaction_driver.asyncio.sleep', new=AsyncMock()):
                    await monitor._monitor('local-task', 'interaction-one')
                self.assertEqual(driver.resume_interaction.await_count, int(vector['expected']))
                driver.mark_interaction_driver_error.assert_not_awaited()

    async def test_lost_resume_reply_retains_claim_across_duplicate_completion_events(self):
        monitor, driver = self.monitor(
            interaction={'state': 'completed', 'resume': {'ready': True, 'completed': False}},
            resume=AsyncMock(side_effect=httpx.ReadTimeout('reply lost')),
        )
        await monitor._monitor('local-task', 'interaction-one')
        await monitor._monitor('local-task', 'interaction-one')
        driver.resume_interaction.assert_awaited_once_with('local-task', 'interaction-one')
        driver.mark_interaction_driver_error.assert_awaited_once()
        driver.close_interaction.assert_not_awaited()

    async def test_cancellation_after_resume_started_cannot_trigger_a_second_resume(self):
        monitor, driver = self.monitor(
            interaction={'state': 'completed', 'resume': {'ready': True}},
            resume=AsyncMock(side_effect=asyncio.CancelledError()),
        )
        with self.assertRaises(asyncio.CancelledError):
            await monitor._monitor('local-task', 'interaction-one')
        await monitor._monitor('local-task', 'interaction-one')
        self.assertEqual(driver.resume_interaction.await_count, 1)
        driver.mark_interaction_driver_error.assert_not_awaited()

    async def test_transport_exhaustion_and_unsafe_calls_keep_their_different_budgets(self):
        for call_class, expected_attempts in (('read', 3), ('prepare', 3), ('unsafe', 1)):
            with self.subTest(call_class=call_class):
                client = AgentBridgeMcpClient(mcp_url='https://agentbridge.invalid/mcp', token_provider=lambda: 'fixture')
                client._call_tool_once = AsyncMock(side_effect=httpx.ReadTimeout('temporary'))
                with patch('reference_host.mcp_client.asyncio.sleep', new=AsyncMock()) as sleep:
                    with self.assertRaises(httpx.ReadTimeout):
                        await client.call_tool('fixture', recovery_class=call_class)
                self.assertEqual(client._call_tool_once.await_count, expected_attempts)
                self.assertEqual(sleep.await_count, expected_attempts - 1)

    async def test_recovery_deadline_prevents_sleep_and_another_attempt(self):
        client = AgentBridgeMcpClient(mcp_url='https://agentbridge.invalid/mcp', token_provider=lambda: 'fixture')
        client._call_tool_once = AsyncMock(side_effect=httpx.ReadTimeout('temporary'))
        with patch('reference_host.mcp_client.SAFE_RECOVERY_DELAYS', (5.0, 5.0)):
            with patch('reference_host.mcp_client.asyncio.sleep', new=AsyncMock()) as sleep:
                with self.assertRaises(httpx.ReadTimeout):
                    await client.call_tool('fixture', recovery_class='read')
        self.assertEqual(client._call_tool_once.await_count, 1)
        sleep.assert_not_awaited()
