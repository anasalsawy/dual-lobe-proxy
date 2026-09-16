# Recipient Routing: Test Scenarios

This document provides concrete test scenarios to validate the recipient routing implementation.

## Scenario 1: Basic Explicit Addressing

**Setup:** 3 agents (data_analyst, backend_agent, ui_developer) with routing enabled

**Turn 1:** `"data_analyst, please summarize the Q3 metrics"`

Expected Results:
- data_analyst: should_respond=true, confidence>=0.85, Output generated
- backend_agent: should_respond=false, confidence>=0.85, Suppressed
- ui_developer: should_respond=false, confidence>=0.85, Suppressed

Validation:
- [ ] Only data_analyst generates output
- [ ] Other agents have events logged
- [ ] Memory updated in all agents

## Scenario 2: Ambiguous Message (Safe Default)

**Turn:** `"Do you think we should invest in optimization?"`

Expected Results:
- All agents respond (ambiguous defaults to yes)
- Confidence scores 0.4-0.8 (lower than explicit)
- Multiple responses acceptable for ambiguous case

Validation:
- [ ] All agents generate output
- [ ] Moderate confidence levels
- [ ] No false suppressions

## Scenario 3: Role-Based Reference

**Turn:** `"whoever handles backend infrastructure, please audit the API"`

Expected Results:
- backend_agent: should_respond=true, confidence 0.75-0.85
- Other agents: should_respond=false, suppressed

Validation:
- [ ] Role inference works (0.75+ confidence)
- [ ] Suppression accurate despite non-explicit mention

## Scenario 4: Broadcast Message

**Turn:** `"everyone, please provide status updates"`

Expected Results:
- All agents respond (broadcast keywords detected)
- Confidence >= 0.90 for all
- No suppressions

Validation:
- [ ] Broadcast detection works
- [ ] All agents respond appropriately

## Scenario 5: Context Leverage After Suppression

**Follow-up Turn:** `"data_analyst, can you relate the optimization to Q3?"`

Expected Results:
- data_analyst responds with context from Turn 1-2
- References suppressed messages
- Memory preserved despite suppressions

Validation:
- [ ] Agent references suppressed context
- [ ] Memory not lost by suppression
- [ ] Full conversation reconstruction possible

## Scenario 6: Threshold Tuning

**Test threshold=0.3 (conservative):**
- Result: Higher suppression rate, fewer overlaps

**Test threshold=0.6 (default):**
- Result: Balanced, recommended

**Test threshold=0.85 (liberal):**
- Result: Lower suppression, more responses

Validation:
- [ ] Lower threshold = more suppressions
- [ ] Higher threshold = more responses

## Scenario 7: Error Handling

**Router fails (LLM timeout, parse error):**

Expected Results:
- All agents: should_respond=true (safe default)
- Event: recipient_route_error logged
- No silent failures

Validation:
- [ ] Graceful fallback
- [ ] Error logged
- [ ] No suppression on error

## Scenario 8: Multi-Agent Coordination

**Workflow:**
1. Turn 1: data_analyst analyzes schema
2. Turn 2: backend_agent optimizes based on Turn 1
3. Turn 3: ui_developer updates UI for both

Expected: Each agent has full context despite suppressions

Validation:
- [ ] Coordination works
- [ ] Context chain preserved
- [ ] No information loss

## Scenario 9: Memory After Suppression

**Validation:** Suppressed messages appear in:
- Event logs
- Context memory
- Available for future references

## Scenario 10: Token Efficiency

**Measure with routing enabled:**
- 40% suppression rate → ~26% token savings
- Routing overhead: ~14% (200 tokens vs 1400)

Validation:
- [ ] Actual token savings within 10% of predicted

## Scenario 11: Latency Impact

**Expected:**
- Router analysis: < 1 second typically
- Off critical path: no impact on response time

Validation:
- [ ] P99 latency < 2s
- [ ] No gateway slowdown

## Configuration Tests

**Test: Default values**
- DUAL_LOBE_AGENT_NAME defaults to "agent"
- Threshold defaults to 0.6

**Test: Custom values**
- Agent name correctly used
- Threshold applied correctly

**Test: Disabled**
- DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=false
- No suppression occurs

## Event Validation

**Verify event structure:**
```json
{
  "kind": "response_suppressed_recipient_routing",
  "actor": "lobe-b",
  "payload": {
    "confidence": 0.92,
    "reasoning": "message addressed to...",
    "speaker": "user",
    "detected_recipients": ["agent_name"]
  }
}
```

Validation:
- [ ] All fields present
- [ ] Confidence valid (0.0-1.0)
- [ ] Recipients not empty
- [ ] Reasoning meaningful

## Sign-Off Checklist

- [ ] Syntax validated
- [ ] Basic scenarios pass
- [ ] Advanced scenarios pass
- [ ] Performance acceptable
- [ ] Configuration works
- [ ] Events logged correctly
- [ ] Error handling works
- [ ] Memory preserved
- [ ] Ready for production

**Date:** _____
**Validator:** _____
