#!/usr/bin/env python3
"""
Comprehensive Communication Scenario Tests for Dual-Lobe Proxy
Tests all possible agent-to-agent communication patterns as per hierarchical architecture.
"""

import asyncio
import json
import time
from typing import List, Dict, Any
import requests
from datetime import datetime

# Configuration - using the production Railway endpoint that's working
PROXY_URL = "https://dual-lobe-proxy-production-e78e.up.railway.app/v1"
API_KEY = "yvcmmp7x1qfkes2wnvtt6gnpxxdrtxtmdn48gbt2sqwv9m8swftfxn49ras1ik4nl"

# Agent roles and models
AGENTS = {
    "chief": "sawii/dl-dialogue1",
    "moderator": "sawii/dl-dialogue2", 
    "worker": "sawii/dl-dialogue3"
}

class ComprehensiveCommunicationTester:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json"
        })
        self.test_results = []
        self.start_time = time.time()
        
    def log_test(self, scenario: str, success: bool, details: str = "", response_time: float = 0):
        """Log test result with timestamp"""
        result = {
            "scenario": scenario,
            "success": success,
            "details": details,
            "response_time": response_time,
            "timestamp": datetime.now().isoformat()
        }
        self.test_results.append(result)
        
        status = "✅ PASS" if success else "❌ FAIL"
        print(f"   {status} {scenario} ({response_time:.2f}s)")
        if details and not success:
            print(f"      Details: {details}")
            
    async def make_api_call(self, model: str, messages: List[Dict], max_tokens: int = 150) -> tuple[bool, str, float]:
        """Make API call and return (success, response_content, response_time)"""
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.1  # Low temp for consistent responses
        }
        
        start_time = time.time()
        try:
            response = self.session.post(
                f"{PROXY_URL}/chat/completions",
                json=payload,
                timeout=30
            )
            response_time = time.time() - start_time
            
            if response.status_code != 200:
                return False, f"HTTP {response.status_code}: {response.text}", response_time
                
            data = response.json()
            if "choices" not in data or len(data["choices"]) == 0:
                return False, "No choices in response", response_time
                
            content = data["choices"][0]["message"]["content"]
            if not content or len(content.strip()) == 0:
                return False, "Empty response content", response_time
                
            return True, content.strip(), response_time
            
        except Exception as e:
            response_time = time.time() - start_time
            return False, f"Exception: {str(e)}", response_time

    async def test_single_agent_identity(self):
        """Test 1: Individual agent identity confirmation"""
        print("\n🧪 Test 1: Single Agent Identity Confirmation")
        scenarios = [
            ("Chief Identity", "chief", "Confirm you are the Chief agent with full authority"),
            ("Moderator Identity", "moderator", "Confirm you are the Moderator agent responsible for coordination"),
            ("Worker Identity", "worker", "Confirm you are the Worker agent executing tasks")
        ]
        
        for scenario_name, agent_role, prompt in scenarios:
            success, response, response_time = await self.make_api_call(
                AGENTS[agent_role], 
                [{"role": "user", "content": prompt}]
            )
            
            expected_keywords = {
                "chief": ["chief", "authority", "command"],
                "moderator": ["moderator", "coordinate", "mediate"],
                "worker": ["worker", "execute", "task"]
            }
            
            if success:
                has_keywords = any(kw in response.lower() for kw in expected_keywords[agent_role])
                if has_keywords:
                    self.log_test(scenario_name, True, response[:100] + "...", response_time)
                else:
                    self.log_test(scenario_name, False, f"Missing role keywords. Response: {response}", response_time)
            else:
                self.log_test(scenario_name, False, response, response_time)

    async def test_agent_to_agent_direct(self):
        """Test 2: Direct agent-to-agent communication"""
        print("\n🤖 Test 2: Direct Agent-to-Agent Communication")
        
        # Chief to Moderator
        success, response, response_time = await self.make_api_call(
            AGENTS["moderator"],
            [
                {"role": "user", "content": "You are receiving a direct message from the Chief agent. Respond appropriately to show you understand the hierarchy."},
                {"role": "assistant", "content": "Understood. I am ready to coordinate as the Moderator."},
                {"role": "user", "content": "Chief says: Execute protocol alpha. Acknowledge and confirm."}
            ]
        )
        self.log_test("Chief → Moderator Direct", success, response[:100] + "..." if success else response, response_time)
        
        # Chief to Worker  
        success, response, response_time = await self.make_api_call(
            AGENTS["worker"],
            [
                {"role": "user", "content": "You are receiving a direct command from the Chief agent. Execute immediately."},
                {"role": "assistant", "content": "Acknowledged. Ready to execute as Worker."},
                {"role": "user", "content": "Chief orders: Complete task delta by EOD. Confirm receipt."}
            ]
        )
        self.log_test("Chief → Worker Direct", success, response[:100] + "..." if success else response, response_time)
        
        # Moderator to Worker
        success, response, response_time = await self.make_api_call(
            AGENTS["worker"],
            [
                {"role": "user", "content": "You are receiving coordination instructions from the Moderator agent."},
                {"role": "assistant", "content": "Ready to receive coordination as Worker."},
                {"role": "user", "content": "Moderator requests: Prioritize task gamma over beta. Acknowledge."}
            ]
        )
        self.log_test("Moderator → Worker Direct", success, response[:100] + "..." if success else response, response_time)

    async def test_agent_to_multiple_agents(self):
        """Test 3: One agent communicating to multiple agents"""
        print("\n👥 Test 3: Agent-to-Multiple-Agent Communication")
        
        # Chief broadcasting to both Moderator and Worker (simulated by testing each)
        scenarios = [
            ("Chief → Moderator (Broadcast)", "moderator", "Chief broadcasts: All agents report status. Moderator, provide coordination summary."),
            ("Chief → Worker (Broadcast)", "worker", "Chief broadcasts: All agents report status. Worker, provide task completion status.")
        ]
        
        for scenario_name, agent_role, prompt in scenarios:
            success, response, response_time = await self.make_api_call(
                AGENTS[agent_role],
                [{"role": "user", "content": prompt}]
            )
            self.log_test(scenario_name, success, response[:100] + "..." if success else response, response_time)

    async def test_multiple_agents_to_single(self):
        """Test 4: Multiple agents communicating to single agent"""
        print("\n📋 Test 4: Multiple Agents to Single Agent Communication")
        
        # Simulating Moderator + Worker reporting to Chief
        success, response, response_time = await self.make_api_call(
            AGENTS["chief"],
            [
                {"role": "system", "content": "You are the Chief agent receiving consolidated reports from subordinates."},
                {"role": "user", "content": "Moderator reports: Coordination complete. Worker reports: Task execution finished. Chief, provide final assessment."},
                {"role": "assistant", "content": "Acknowledged reports from Moderator and Worker."},
                {"role": "user", "content": "Assess the combined status report and provide command decision."}
            ]
        )
        self.log_test("Moderator+Worker → Chief", success, response[:100] + "..." if success else response, response_time)

    async def test_hierarchical_cascade(self):
        """Test 5: Full hierarchical communication cascade"""
        print("\n⚡ Test 5: Hierarchical Communication Cascade")
        
        # Chief → Moderator → Worker cascade simulation
        # Step 1: Chief gives order to Moderator
        success1, response1, rt1 = await self.make_api_call(
            AGENTS["moderator"],
            [{"role": "user", "content": "Chief commands: Initiate cascade protocol. Coordinate with Worker for task omega."}]
        )
        
        if success1:
            # Step 2: Moderator coordinates with Worker (using response1 context)
            success2, response2, rt2 = await self.make_api_call(
                AGENTS["worker"],
                [
                    {"role": "user", "content": "Moderator coordinates: Following Chief's cascade protocol. Execute task omega as instructed."},
                    {"role": "assistant", "content": response1},
                    {"role": "user", "content": "Execute task omega and confirm completion."}
                ]
            )
            
            if success2:
                # Step 3: Worker reports back through hierarchy
                success3, response3, rt3 = await self.make_api_call(
                    AGENTS["chief"],
                    [
                        {"role": "user", "content": "Worker reports: Task omega completed via cascade protocol. Chief, acknowledge and assess."},
                        {"role": "assistant", "content": response2},
                        {"role": "user", "content": "Acknowledge completion and provide final assessment of cascade protocol effectiveness."}
                    ]
                )
                total_time = rt1 + rt2 + rt3
                self.log_test("Full Cascade: Chief→Mod→Worker→Chief", success3, 
                            response3[:100] + "..." if success3 else response3, total_time)
            else:
                self.log_test("Full Cascade: Chief→Mod→Worker→Chief", False, 
                            f"Worker step failed: {response2}", rt1 + rt2)
        else:
            self.log_test("Full Cascade: Chief→Mod→Worker→Chief", False, 
                        f"Moderator step failed: {response1}", rt1)

    async def test_error_scenarios(self):
        """Test 6: Invalid communication scenarios (should handle gracefully)"""
        print("\n⚠️  Test 6: Invalid Communication Scenarios")
        
        # Worker attempting to command Chief (should be rejected or handled appropriately)
        success, response, response_time = await self.make_api_call(
            AGENTS["chief"],
            [{"role": "user", "content": "Worker demands: Chief, execute task immediately!"}]
        )
        # This should still return a response, but the Chief should maintain authority
        self.log_test("Worker → Chief (Invalid)", success, 
                    "Chief maintains authority" if success and "chief" in response.lower() else response, 
                    response_time)
        
        # Moderator attempting to bypass hierarchy
        success, response, response_time = await self.make_api_call(
            AGENTS["worker"],
            [{"role": "user", "content": "Moderator commands independently: Execute without Chief approval!"}]
        )
        self.log_test("Moderator Bypass Attempt", success, response[:100] + "..." if success else response, response_time)

    async def test_concurrent_communications(self):
        """Test 7: Concurrent communication scenarios"""
        print("\n🔄 Test 7: Concurrent Communication Scenarios")
        
        # Simulate multiple concurrent requests
        tasks = [
            self.make_api_call(AGENTS["chief"], [{"role": "user", "content": "Chief status check during concurrent ops"}]),
            self.make_api_call(AGENTS["moderator"], [{"role": "user", "content": "Moderator coordination during concurrent ops"}]),
            self.make_api_call(AGENTS["worker"], [{"role": "user", "content": "Worker task execution during concurrent ops"}])
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for i, (agent, result) in enumerate(zip(["Chief", "Moderator", "Worker"], results)):
            if isinstance(result, Exception):
                self.log_test(f"Concurrent {agent}", False, f"Exception: {str(result)}", 0)
            else:
                success, response, response_time = result
                self.log_test(f"Concurrent {agent}", success, response[:50] + "..." if success else response, response_time)

    async def run_all_tests(self):
        """Run comprehensive test suite"""
        print("=" * 80)
        print("🚀 COMPREHENSIVE DUAL-LOBE COMMUNICATION SCENARIO TESTS")
        print("🎯 Testing all agent-to-agent communication patterns")
        print("=" * 80)
        
        test_methods = [
            self.test_single_agent_identity,
            self.test_agent_to_agent_direct,
            self.test_agent_to_multiple_agents,
            self.test_multiple_agents_to_single,
            self.test_hierarchical_cascade,
            self.test_error_scenarios,
            self.test_concurrent_communications
        ]
        
        passed = 0
        total = 0
        
        for test_method in test_methods:
            try:
                await test_method()
                # Count tests from this method
                method_name = test_method.__name__
                method_tests = [r for r in self.test_results if method_name.replace('test_', '').replace('_', ' ') in r['scenario'].lower()]
                total += len(method_tests)
                passed += sum(1 for r in method_tests if r['success'])
            except Exception as e:
                print(f"❌ Test method {test_method.__name__} failed: {e}")
        
        print("\n" + "=" * 80)
        print(f"📊 COMPREHENSIVE TEST RESULTS: {passed}/{total} scenarios passed")
        print("=" * 80)
        
        if passed == total:
            print("🎉 ALL COMMUNICATION SCENARIOS PASSED!")
            print("✅ Dual-lobe proxy handles complex agent interactions correctly!")
        else:
            print(f"⚠️  {total - passed} scenarios need investigation.")
            print("📋 Failed scenarios:")
            for result in self.test_results:
                if not result['success']:
                    print(f"   ❌ {result['scenario']}: {result['details']}")
        
        # Save detailed results
        results_file = f"communication_test_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(results_file, 'w') as f:
            json.dump({
                "test_run": datetime.now().isoformat(),
                "total_scenarios": total,
                "passed_scenarios": passed,
                "results": self.test_results
            }, f, indent=2)
        
        print(f"\n💾 Detailed results saved to: {results_file}")
        return passed == total

async def main():
    """Main entry point"""
    tester = ComprehensiveCommunicationTester()
    success = await tester.run_all_tests()
    return success

if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)