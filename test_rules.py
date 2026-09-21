#!/usr/bin/env python3
"""
Test dynamic rule parsing functionality.
"""

import sys
sys.path.insert(0, 'src')

from dual_lobe.b.rules import parse_rule_command, add_rule, get_active_rules, clear_rules

def test_rule_parsing():
    """Test rule command parsing."""
    print("=" * 60)
    print("🎯 DYNAMIC RULE PARSING TESTS")
    print("=" * 60)
    
    # Clear any existing rules
    clear_rules("Research agent")
    clear_rules("IT agent")
    
    test_cases = [
        (
            "Simple mute command",
            "Research agent, don't speak",
            ("Research agent", "don't speak")
        ),
        (
            "Keep working command", 
            "IT agent, keep working and don't speak",
            ("IT agent", "keep working and don't speak")
        ),
        (
            "Conditional response command",
            "Worker Bob, only respond if there's an emergency",
            ("Worker Bob", "only respond if there's an emergency")
        ),
        (
            "Not a rule command",
            "Hello everyone!",
            None
        ),
        (
            "Regular conversation",
            "What's the status?",
            None
        ),
    ]
    
    passed = 0
    total = len(test_cases)
    
    for description, message, expected in test_cases:
        print(f"\n🧪 {description}")
        print(f"   Message: \"{message}\"")
        result = parse_rule_command(message)
        print(f"   Expected: {expected}")
        print(f"   Got: {result}")
        
        if result == expected:
            print("   ✅ PASS")
            passed += 1
        else:
            print("   ❌ FAIL")
    
    # Test rule storage
    print(f"\n🧪 Testing rule storage and retrieval")
    add_rule("Research agent", "keep working and don't speak unless directly addressed")
    rules = get_active_rules("Research agent")
    if len(rules) == 1 and rules[0].rule_text == "keep working and don't speak unless directly addressed":
        print("   ✅ Rule storage works")
        passed += 1
        total += 1
    else:
        print("   ❌ Rule storage failed")
        total += 1
    
    print("\n" + "=" * 60)
    print(f"📈 RESULTS: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 ALL RULE PARSING TESTS PASSED!")
        return True
    else:
        print(f"⚠️  {total - passed} tests failed")
        return False

if __name__ == "__main__":
    success = test_rule_parsing()
    sys.exit(0 if success else 1)