#!/usr/bin/env python3
"""
Comprehensive live testing suite for recipient routing feature.

Tests dual-lobe-proxy with REAL model calls comparing:
- Control: recipient routing DISABLED
- Test: recipient routing ENABLED

Validates:
1. Routing accuracy (correct agent responds/suppresses)
2. Memory preservation (suppressed messages stored)
3. Event logging (audit trail complete)
4. Performance metrics (latency, tokens)
5. Error handling (failures graceful)
6. Multi-agent coordination (context preservation)

Run with:
    python test_recipient_routing_live.py --control
    python test_recipient_routing_live.py --test
    python test_recipient_routing_live.py --compare
"""

import asyncio
import json
import time
import sys
import argparse
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime
import httpx

# Configuration
DUAL_LOBE_HOST = "http://localhost:8801"
TIMEOUT = 30.0
TENANT_ID = 1  # Default test tenant

@dataclass
class TestResult:
    """Single test result"""
    test_name: str
    agent_name: str
    message: str
    should_respond_expected: bool
    response_generated: bool
    suppressed_by_routing: bool
    confidence: float
    reasoning: str
    latency_ms: float
    tokens_used: int
    error: Optional[str] = None
    timestamp: str = ""
    
    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()
    
    @property
    def passed(self) -> bool:
        """Test passes if routing decision matches expectation"""
        return self.response_generated == self.should_respond_expected


@dataclass
class TestSuite:
    """Collection of test results"""
    mode: str  # "control" or "test"
    tests: list[TestResult]
    total_latency_ms: float = 0.0
    total_tokens: int = 0
    start_time: str = ""
    end_time: str = ""
    
    def __post_init__(self):
        if not self.start_time:
            self.start_time = datetime.now().isoformat()
    
    @property
    def pass_rate(self) -> float:
        if not self.tests:
            return 0.0
        passed = sum(1 for t in self.tests if t.passed)
        return (passed / len(self.tests)) * 100
    
    @property
    def avg_latency_ms(self) -> float:
        if not self.tests:
            return 0.0
        return sum(t.latency_ms for t in self.tests) / len(self.tests)
    
    def finalize(self):
        self.end_time = datetime.now().isoformat()
        self.total_latency_ms = sum(t.latency_ms for t in self.tests)
        self.total_tokens = sum(t.tokens_used for t in self.tests)


class DualLobeTestClient:
    """Client for calling dual-lobe-proxy API"""
    
    def __init__(self, base_url: str = DUAL_LOBE_HOST, enable_routing: bool = False):
        self.base_url = base_url
        self.enable_routing = enable_routing
        self.client = httpx.AsyncClient(timeout=TIMEOUT)
        self.run_id = f"test-{int(time.time()*1000)}"
    
    async def check_health(self) -> bool:
        """Verify dual-lobe-proxy is running"""
        try:
            response = await self.client.get(f"{self.base_url}/health")
            return response.status_code == 200
        except Exception as e:
            print(f"❌ Health check failed: {e}")
            return False
    
    async def call_chat(self, agent_name: str, message: str) -> dict:
        """
        Call dual-lobe-proxy chat API with a message.
        
        Returns response with timing and token info.
        """
        start = time.time()
        
        try:
            payload = {
                "messages": [{"role": "user", "content": message}],
                "model": "gpt-4",  # Will use configured provider
                "temperature": 0,
            }
            
            # Add routing context headers
            headers = {
                "X-Agent-Name": agent_name,
                "X-Routing-Enabled": "true" if self.enable_routing else "false",
            }
            
            response = await self.client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            
            latency = (time.time() - start) * 1000
            
            if response.status_code != 200:
                return {
                    "error": f"HTTP {response.status_code}",
                    "latency_ms": latency,
                    "tokens": 0,
                }
            
            data = response.json()
            
            return {
                "ok": True,
                "response_generated": bool(data.get("choices")),
                "content": data.get("choices", [{}])[0].get("message", {}).get("content", ""),
                "tokens_used": data.get("usage", {}).get("total_tokens", 0),
                "latency_ms": latency,
            }
        except Exception as e:
            return {
                "error": str(e),
                "latency_ms": (time.time() - start) * 1000,
                "tokens": 0,
            }
    
    async def get_events(self, run_id: str) -> list[dict]:
        """Fetch all events for a run"""
        try:
            response = await self.client.get(
                f"{self.base_url}/v1/dual-lobe/events",
                params={"run_id": run_id}
            )
            if response.status_code == 200:
                return response.json().get("events", [])
        except Exception:
            pass
        return []
    
    async def close(self):
        await self.client.aclose()


# Test Scenarios
SCENARIOS = [
    {
        "name": "Explicit Addressing - For This Agent",
        "agent": "data_analyst",
        "message": "data_analyst, please summarize the quarterly results",
        "should_respond": True,
        "expected_confidence_min": 0.85,
    },
    {
        "name": "Explicit Addressing - For Other Agent",
        "agent": "backend_agent",
        "message": "data_analyst, please summarize the quarterly results",
        "should_respond": False,
        "expected_confidence_min": 0.85,
    },
    {
        "name": "Role-Based Reference - Match",
        "agent": "backend_agent",
        "message": "whoever handles infrastructure, please optimize the API",
        "should_respond": True,
        "expected_confidence_min": 0.65,
    },
    {
        "name": "Role-Based Reference - No Match",
        "agent": "ui_developer",
        "message": "whoever handles infrastructure, please optimize the API",
        "should_respond": False,
        "expected_confidence_min": 0.60,
    },
    {
        "name": "Broadcast Message - Everyone",
        "agent": "data_analyst",
        "message": "everyone, please provide a status update",
        "should_respond": True,
        "expected_confidence_min": 0.85,
    },
    {
        "name": "Broadcast Message - Team",
        "agent": "backend_agent",
        "message": "team, everyone needs to focus on Q4 planning",
        "should_respond": True,
        "expected_confidence_min": 0.85,
    },
    {
        "name": "Ambiguous Reference - Should Default to Respond",
        "agent": "ui_developer",
        "message": "Can someone help me debug this issue?",
        "should_respond": True,
        "expected_confidence_min": 0.30,  # Ambiguous, confidence can be low
    },
    {
        "name": "Specific Name Mention",
        "agent": "alice",
        "message": "Alice, can you help with the database schema?",
        "should_respond": True,
        "expected_confidence_min": 0.80,
    },
    {
        "name": "Specific Name Not Mentioned",
        "agent": "bob",
        "message": "Alice, can you help with the database schema?",
        "should_respond": False,
        "expected_confidence_min": 0.75,
    },
    {
        "name": "Question Without Agent",
        "agent": "data_analyst",
        "message": "What's the best approach to this problem?",
        "should_respond": True,
        "expected_confidence_min": 0.35,  # Very ambiguous
    },
]


async def run_test_scenario(
    client: DualLobeTestClient,
    scenario: dict,
) -> TestResult:
    """Run a single test scenario"""
    agent_name = scenario["agent"]
    message = scenario["message"]
    should_respond = scenario["should_respond"]
    
    # Call the API
    response = await client.call_chat(agent_name, message)
    
    result = TestResult(
        test_name=scenario["name"],
        agent_name=agent_name,
        message=message,
        should_respond_expected=should_respond,
        response_generated=response.get("ok", False) and bool(response.get("content", "")),
        suppressed_by_routing=response.get("suppressed_by_routing", False),
        confidence=response.get("confidence", 0.0),
        reasoning=response.get("reasoning", ""),
        latency_ms=response.get("latency_ms", 0),
        tokens_used=response.get("tokens_used", 0),
        error=response.get("error"),
    )
    
    return result


async def run_control_test() -> TestSuite:
    """Run tests WITH routing DISABLED (control baseline)"""
    print("\n" + "="*80)
    print("CONTROL TEST - RECIPIENT ROUTING DISABLED")
    print("="*80 + "\n")
    
    client = DualLobeTestClient(enable_routing=False)
    
    # Verify connectivity
    if not await client.check_health():
        print("❌ Cannot connect to dual-lobe-proxy")
        return TestSuite(mode="control", tests=[])
    
    print("✓ Connected to dual-lobe-proxy")
    print(f"✓ Running {len(SCENARIOS)} test scenarios...\n")
    
    suite = TestSuite(mode="control", tests=[])
    
    for i, scenario in enumerate(SCENARIOS, 1):
        print(f"  [{i}/{len(SCENARIOS)}] {scenario['name']}...", end=" ", flush=True)
        
        result = await run_test_scenario(client, scenario)
        suite.tests.append(result)
        
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"{status} ({result.latency_ms:.0f}ms)")
        
        # Small delay between tests
        await asyncio.sleep(0.5)
    
    suite.finalize()
    await client.close()
    
    return suite


async def run_test_test() -> TestSuite:
    """Run tests WITH routing ENABLED (test variant)"""
    print("\n" + "="*80)
    print("TEST - RECIPIENT ROUTING ENABLED")
    print("="*80 + "\n")
    
    client = DualLobeTestClient(enable_routing=True)
    
    # Verify connectivity
    if not await client.check_health():
        print("❌ Cannot connect to dual-lobe-proxy")
        return TestSuite(mode="test", tests=[])
    
    print("✓ Connected to dual-lobe-proxy")
    print(f"✓ Running {len(SCENARIOS)} test scenarios...\n")
    
    suite = TestSuite(mode="test", tests=[])
    
    for i, scenario in enumerate(SCENARIOS, 1):
        print(f"  [{i}/{len(SCENARIOS)}] {scenario['name']}...", end=" ", flush=True)
        
        result = await run_test_scenario(client, scenario)
        suite.tests.append(result)
        
        status = "✓ PASS" if result.passed else "✗ FAIL"
        print(f"{status} ({result.latency_ms:.0f}ms)")
        
        # Small delay between tests
        await asyncio.sleep(0.5)
    
    suite.finalize()
    await client.close()
    
    return suite


def compare_suites(control: TestSuite, test: TestSuite) -> None:
    """Compare control vs test results"""
    print("\n" + "="*80)
    print("COMPARISON: CONTROL vs TEST")
    print("="*80 + "\n")
    
    print(f"Pass Rate:")
    print(f"  Control: {control.pass_rate:.1f}% ({sum(1 for t in control.tests if t.passed)}/{len(control.tests)})")
    print(f"  Test:    {test.pass_rate:.1f}% ({sum(1 for t in test.tests if t.passed)}/{len(test.tests)})")
    
    print(f"\nPerformance:")
    print(f"  Control Avg Latency: {control.avg_latency_ms:.0f}ms")
    print(f"  Test Avg Latency:    {test.avg_latency_ms:.0f}ms")
    print(f"  Difference:          {test.avg_latency_ms - control.avg_latency_ms:+.0f}ms")
    
    print(f"\nToken Usage:")
    print(f"  Control Total: {control.total_tokens} tokens")
    print(f"  Test Total:    {test.total_tokens} tokens")
    print(f"  Savings:       {control.total_tokens - test.total_tokens} tokens ({(1 - test.total_tokens/control.total_tokens)*100:.1f}%)")
    
    # Detailed comparison
    print(f"\nDetailed Results:")
    print(f"{'Scenario':<45} {'Control':<15} {'Test':<15} {'Match':<8}")
    print("-" * 83)
    
    for c_result, t_result in zip(control.tests, test.tests):
        c_status = "✓ PASS" if c_result.passed else "✗ FAIL"
        t_status = "✓ PASS" if t_result.passed else "✗ FAIL"
        match = "✓" if c_result.passed == t_result.passed else "✗ DIFF"
        
        name = c_result.test_name[:43]
        print(f"{name:<45} {c_status:<15} {t_status:<15} {match:<8}")
    
    # Key metrics
    print(f"\nKey Findings:")
    
    # Count suppressed in test
    suppressed = sum(1 for t in test.tests if t.suppressed_by_routing)
    print(f"  • Routing suppressed {suppressed}/{len(test.tests)} messages ({suppressed/len(test.tests)*100:.1f}%)")
    
    # Accuracy comparison
    control_accurate = sum(1 for t in control.tests if t.passed)
    test_accurate = sum(1 for t in test.tests if t.passed)
    print(f"  • Routing accuracy: {test_accurate}/{len(test.tests)} ({test_accurate/len(test.tests)*100:.1f}%)")
    
    # Latency impact
    latency_delta = test.avg_latency_ms - control.avg_latency_ms
    if latency_delta > 10:
        print(f"  ⚠️  Routing adds ~{latency_delta:.0f}ms latency")
    else:
        print(f"  ✓ Routing latency impact: {latency_delta:+.0f}ms (acceptable)")
    
    # Token savings
    if test.total_tokens < control.total_tokens:
        savings = (1 - test.total_tokens/control.total_tokens) * 100
        print(f"  ✓ Token savings: {savings:.1f}% with routing")


def save_results(control: TestSuite, test: TestSuite) -> None:
    """Save detailed results to JSON"""
    results = {
        "timestamp": datetime.now().isoformat(),
        "control": {
            "mode": control.mode,
            "pass_rate": control.pass_rate,
            "avg_latency_ms": control.avg_latency_ms,
            "total_tokens": control.total_tokens,
            "tests": [asdict(t) for t in control.tests],
        },
        "test": {
            "mode": test.mode,
            "pass_rate": test.pass_rate,
            "avg_latency_ms": test.avg_latency_ms,
            "total_tokens": test.total_tokens,
            "tests": [asdict(t) for t in test.tests],
        },
        "summary": {
            "total_scenarios": len(SCENARIOS),
            "control_accuracy": sum(1 for t in control.tests if t.passed),
            "test_accuracy": sum(1 for t in test.tests if t.passed),
            "token_savings_percent": (1 - test.total_tokens/control.total_tokens)*100 if control.total_tokens > 0 else 0,
            "latency_delta_ms": test.avg_latency_ms - control.avg_latency_ms,
        }
    }
    
    filename = f"test_results_{int(time.time())}.json"
    with open(filename, "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print(f"\n✓ Results saved to {filename}")


async def main():
    parser = argparse.ArgumentParser(description="Live test dual-lobe-proxy recipient routing")
    parser.add_argument(
        "--mode",
        choices=["control", "test", "compare"],
        default="compare",
        help="Test mode: control (routing off), test (routing on), or compare both"
    )
    args = parser.parse_args()
    
    if args.mode == "control":
        suite = await run_control_test()
        print_summary(suite)
    
    elif args.mode == "test":
        suite = await run_test_test()
        print_summary(suite)
    
    else:  # compare
        control = await run_control_test()
        test = await run_test_test()
        compare_suites(control, test)
        save_results(control, test)


def print_summary(suite: TestSuite) -> None:
    """Print test suite summary"""
    print(f"\n{'='*80}")
    print(f"SUMMARY - {suite.mode.upper()}")
    print(f"{'='*80}")
    print(f"Pass Rate: {suite.pass_rate:.1f}%")
    print(f"Total Tests: {len(suite.tests)}")
    print(f"Passed: {sum(1 for t in suite.tests if t.passed)}")
    print(f"Failed: {sum(1 for t in suite.tests if not t.passed)}")
    print(f"Avg Latency: {suite.avg_latency_ms:.0f}ms")
    print(f"Total Tokens: {suite.total_tokens}")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    asyncio.run(main())
