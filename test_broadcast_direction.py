#!/usr/bin/env python3
"""
Simplified test for broadcast direction logic.

Tests the core _apply_routing_mode function directly.
"""

import sys
sys.path.insert(0, 'src')

from dual_lobe.b.recipient_router import _apply_routing_mode, RecipientAnalysis
from dual_lobe.b.recipient_router import parse_hierarchy_roles

def test_broadcast_direction():
    """Test broadcast direction rules."""
    print("=" * 60)
    print("🎯 BROADCAST DIRECTION TESTS")
    print("=" * 60)
    
    hierarchy = parse_hierarchy_roles("chief:0,moderators:1,workers:2")
    mode = "hierarchy"
    
    # Test cases: (description, analysis, agent_name, expected_should_respond)
    test_cases = [
        # Human broadcasts - only highest rank responds
        (
            "Human broadcast - chief should respond",
            RecipientAnalysis(True, 0.9, "test", "user", ["chief", "moderators", "workers"], True),
            "chief",
            True
        ),
        (
            "Human broadcast - moderator should NOT respond", 
            RecipientAnalysis(True, 0.9, "test", "user", ["chief", "moderators", "workers"], True),
            "moderator_alice",
            False
        ),
        (
            "Human broadcast - worker should NOT respond",
            RecipientAnalysis(True, 0.9, "test", "user", ["chief", "moderators", "workers"], True),
            "worker_bob",
            False
        ),
        
        # Downward broadcasts - should work
        (
            "Chief broadcast to moderators - moderator should respond",
            RecipientAnalysis(True, 0.9, "test", "chief", ["moderators"], True),
            "moderators",  # Use exact role name
            True
        ),
        (
            "Moderator broadcast to workers - worker should respond",
            RecipientAnalysis(True, 0.9, "test", "moderators", ["workers"], True),
            "workers",  # Use exact role name
            True
        ),
        
        # Upward broadcasts - should be blocked
        (
            "Worker broadcast to moderators - should be blocked",
            RecipientAnalysis(True, 0.9, "test", "worker_bob", ["moderators"], True),
            "moderator_alice",
            False
        ),
        (
            "Moderator broadcast to chief - should be blocked",
            RecipientAnalysis(True, 0.9, "test", "moderator_alice", ["chief"], True),
            "chief",
            False
        ),
        
        # Same-level broadcasts - should be blocked
        (
            "Moderator broadcast to moderators - same level blocked",
            RecipientAnalysis(True, 0.9, "test", "moderator_alice", ["moderators"], True),
            "moderator_bob",
            False
        ),
        
        # Non-broadcast (direct addressing) - should always work
        (
            "Direct address - should always work (broadcast=False)",
            RecipientAnalysis(True, 0.9, "test", "worker_bob", ["chief"], False),
            "chief",
            True
        ),
    ]
    
    passed = 0
    total = len(test_cases)
    
    for description, analysis, agent_name, expected in test_cases:
        print(f"\n🧪 {description}")
        result = _apply_routing_mode(analysis, mode, agent_name, hierarchy)
        print(f"   Expected: {expected}, Got: {result.should_respond}")
        print(f"   Reasoning: {result.reasoning}")
        
        if result.should_respond == expected:
            print("   ✅ PASS")
            passed += 1
        else:
            print("   ❌ FAIL")
    
    print("\n" + "=" * 60)
    print(f"📈 RESULTS: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 ALL BROADCAST DIRECTION TESTS PASSED!")
        return True
    else:
        print(f"⚠️  {total - passed} tests failed")
        return False

if __name__ == "__main__":
    success = test_broadcast_direction()
    sys.exit(0 if success else 1)