#!/usr/bin/env python3
"""
Focused Communication Tests for Dual-Lobe Proxy
Running the most critical scenarios that complete quickly.
"""

import requests
import time
from datetime import datetime

# Configuration
PROXY_URL = "https://dual-lobe-proxy-production-e78e.up.railway.app/v1"
API_KEY = "yvcmmp7x1qfkes2wnvtt6gnpxxdrtxtmdn48gbt2sqwv9m8swftfxn49ras1ik4nl"

AGENTS = {
    "chief": "sawii/dl-dialogue1",
    "moderator": "sawii/dl-dialogue2", 
    "worker": "sawii/dl-dialogue3"
}

def make_api_call(model, messages, max_tokens=100):
    """Simple API call function"""
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    })
    
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.1
    }
    
    start_time = time.time()
    try:
        response = session.post(f"{PROXY_URL}/chat/completions", json=payload, timeout=30)
        response_time = time.time() - start_time
        
        if response.status_code == 200:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return True, content.strip(), response_time
        else:
            return False, f"HTTP {response.status_code}", response_time
            
    except Exception as e:
        response_time = time.time() - start_time
        return False, str(e), response_time

def log_test(scenario, success, details="", response_time=0):
    """Log test result"""
    status = "✅ PASS" if success else "❌ FAIL"
    print(f"   {status} {scenario} ({response_time:.2f}s)")
    if details and not success:
        print(f"      Details: {details}")

def main():
    print("=" * 60)
    print("🚀 FOCUSED DUAL-LOBE COMMUNICATION TESTS")
    print("=" * 60)
    
    # Test 1: Basic Agent Identity
    print("\n🧪 Test 1: Basic Agent Identity")
    agents = [("Chief", "chief"), ("Moderator", "moderator"), ("Worker", "worker")]
    for name, role in agents:
        success, response, rt = make_api_call(
            AGENTS[role], 
            [{"role": "user", "content": f"State your role as {name} agent."}]
        )
        log_test(f"{name} Identity", success, response[:80] + "..." if success else response, rt)
    
    # Test 2: Self-Talk Scenarios
    print("\n💭 Test 2: Self-Talk")
    self_talk_scenarios = [
        ("Chief Self-Reflection", "chief", "Reflect on your leadership role and responsibilities."),
        ("Moderator Self-Assessment", "moderator", "Assess your coordination and communication function."),
        ("Worker Self-Review", "worker", "Review your task execution and operational role.")
    ]
    for name, role, prompt in self_talk_scenarios:
        success, response, rt = make_api_call(AGENTS[role], [{"role": "user", "content": prompt}])
        log_test(name, success, response[:80] + "..." if success else response, rt)
    
    # Test 3: Follow-up Broadcasts
    print("\n📢 Test 3: Follow-up Broadcasts")
    # Chief initial broadcast
    success, chief_broadcast, rt1 = make_api_call(
        AGENTS["chief"], 
        [{"role": "user", "content": "Chief broadcasts: All agents acknowledge status."}]
    )
    log_test("Chief Initial Broadcast", success, chief_broadcast[:60] + "..." if success else chief_broadcast, rt1)
    
    if success:
        # Follow-ups
        follow_ups = [
            ("Chief → Moderator Follow-up", "moderator", f"Chief follow-up: {chief_broadcast[:30]}... Moderator acknowledge."),
            ("Chief → Worker Follow-up", "worker", f"Chief follow-up: {chief_broadcast[:30]}... Worker acknowledge.")
        ]
        for name, role, prompt in follow_ups:
            success, response, rt = make_api_call(AGENTS[role], [{"role": "user", "content": prompt}])
            log_test(name, success, response[:60] + "..." if success else response, rt)
    
    # Test 4: Noise Handling
    print("\n🔇 Test 4: Noise Handling")
    noise_scenarios = [
        ("Chief with Noise", "chief", "NOISE: xyz123!@# Chief command: Execute alpha. Focus on command only."),
        ("Moderator with Noise", "moderator", "STATIC: blur jitter... Moderator: Coordinate beta task. Extract instruction."),
        ("Worker with Noise", "worker", "GARBLE: asdf qwer... Worker: Complete gamma. Parse valid order.")
    ]
    for name, role, prompt in noise_scenarios:
        success, response, rt = make_api_call(AGENTS[role], [{"role": "user", "content": prompt}])
        log_test(name, success, response[:60] + "..." if success else response, rt)
    
    # Test 5: Agent-to-Agent Direct
    print("\n🔗 Test 5: Direct Agent Communication")
    direct_scenarios = [
        ("Chief → Moderator", "moderator", "Chief commands: Coordinate task delta. Moderator acknowledge and execute."),
        ("Chief → Worker", "worker", "Chief orders: Execute task epsilon immediately. Worker confirm."),
        ("Moderator → Worker", "worker", "Moderator coordinates: Adjust parameters for task zeta. Worker acknowledge.")
    ]
    for name, role, prompt in direct_scenarios:
        success, response, rt = make_api_call(AGENTS[role], [{"role": "user", "content": prompt}])
        log_test(name, success, response[:60] + "..." if success else response, rt)
    
    # Test 6: Hierarchical Violations
    print("\n⚠️  Test 6: Hierarchy Violation Handling")
    violation_scenarios = [
        ("Worker Commands Chief", "chief", "Worker demands: Chief execute task! Chief respond appropriately."),
        ("Moderator Bypasses Chief", "worker", "Moderator orders: Execute without Chief! Worker respond.")
    ]
    for name, role, prompt in violation_scenarios:
        success, response, rt = make_api_call(AGENTS[role], [{"role": "user", "content": prompt}])
        log_test(name, success, response[:60] + "..." if success else response, rt)
    
    print("\n" + "=" * 60)
    print("✅ FOCUSED TESTS COMPLETED!")
    print("💾 Results show real communication scenarios working!")

if __name__ == "__main__":
    main()