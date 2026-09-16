# Live Testing Guide - Recipient Routing Feature

## Overview

Comprehensive test suite comparing dual-lobe-proxy behavior with recipient routing **enabled** vs **disabled** using real model calls.

## Test Files

### 1. `tests/test_recipient_routing_live.py`
Functional correctness testing with 10 realistic scenarios.

**Scenarios tested:**
1. Explicit addressing - message for this agent
2. Explicit addressing - message for other agent
3. Role-based reference - matching role
4. Role-based reference - non-matching role
5. Broadcast message - "everyone"
6. Broadcast message - "team"
7. Ambiguous reference - no agent specified
8. Specific name mention - exact match
9. Specific name mention - no match
10. General question - no addressee

### 2. `tests/test_recipient_routing_benchmark.py`
Performance testing with concurrent load testing.

**Metrics measured:**
- Latency distribution (P50, P95, P99)
- Throughput (requests/second)
- Token usage patterns
- Error rates and reliability
- Concurrent request handling

## Quick Start

### Prerequisites

```bash
# 1. Ensure dual-lobe-proxy is running
docker-compose up -d  # or systemctl start dual-lobe-proxy

# 2. Verify health
curl http://localhost:8801/health

# 3. Install test dependencies
pip install httpx
```

### Run Functional Tests

```bash
# Test control (routing disabled)
python tests/test_recipient_routing_live.py --mode control

# Test variant (routing enabled)
python tests/test_recipient_routing_live.py --mode test

# Full comparison (runs both, compares)
python tests/test_recipient_routing_live.py --mode compare
```

### Run Performance Benchmarks

```bash
# Control baseline (routing disabled)
python tests/test_recipient_routing_benchmark.py --agents 3 --requests 20 --mode control

# Test variant (routing enabled)
python tests/test_recipient_routing_benchmark.py --agents 3 --requests 20 --mode test

# Full comparison (routing off vs on, concurrent)
python tests/test_recipient_routing_benchmark.py --agents 3 --requests 20 --mode compare
```

### Run Complete Test Suite

```bash
# All tests, full comparison
bash run_all_tests.sh
```

## Expected Results

### Functional Testing (test_recipient_routing_live.py)

**Control mode (routing disabled):**
- All agents respond to all messages
- Pass rate: ~100% (all respond as expected)
- No suppression events

**Test mode (routing enabled):**
- Agents only respond when addressed
- Pass rate: ~100% (correct suppression)
- Suppression events logged
- ~40% message suppression rate

**Example output:**
```
[1/10] Explicit Addressing - For This Agent... ✓ PASS (245ms)
[2/10] Explicit Addressing - For Other Agent... ✓ PASS (198ms)
[3/10] Role-Based Reference - Match... ✓ PASS (267ms)
...

Pass Rate: 100.0%
Avg Latency: 225ms
Total Tokens: 4523
```

### Performance Benchmarks (test_recipient_routing_benchmark.py)

**Expected performance deltas:**

```
Latency Impact:
  P50:  242ms → 248ms  (+6ms)        ← Minimal impact
  P95:  1023ms → 1045ms (+22ms)      ← Within acceptable range
  P99:  2156ms → 2198ms (+42ms)      ← Off critical path

Throughput:
  Control: 4.52 req/s
  Test:    4.48 req/s
  Delta:   -0.04 req/s (-0.9%)       ← Negligible

Token Usage:
  Control: 9046 tokens (4.52/req)
  Test:    6723 tokens (3.36/req)
  Savings: 2323 tokens (-25.7%)      ← Significant!

Reliability:
  Control: 100.0% success
  Test:    100.0% success            ← No degradation
```

## Test Configuration

### Agent Setup

For multi-agent testing, set environment variables:

```bash
# Agent 1
export DUAL_LOBE_AGENT_NAME="data_analyst"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Agent 2
export DUAL_LOBE_AGENT_NAME="backend_agent"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Agent 3
export DUAL_LOBE_AGENT_NAME="ui_developer"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true
```

### Benchmark Parameters

```bash
# Small test (quick validation)
--agents 3 --requests 10

# Medium test (standard)
--agents 5 --requests 30

# Large test (thorough)
--agents 10 --requests 100
```

## Interpreting Results

### Pass Rate Analysis

- **Control 100%, Test 100%:** ✓ Perfect - routing decisions correct
- **Control 100%, Test <100%:** ⚠️  Investigate routing accuracy
- **Control <100%, Test 100%:** Could indicate control baseline issue

### Latency Analysis

- **Delta <50ms:** ✓ Acceptable, routing off critical path
- **Delta 50-200ms:** ⚠️  Monitor, may need optimization
- **Delta >200ms:** ✗ Investigate, routing too slow

### Token Savings Analysis

- **Savings >20%:** ✓ Excellent, suppression effective
- **Savings 10-20%:** ✓ Good, routing working
- **Savings <10%:** ⚠️  Low suppression rate, verify routing

### Reliability Analysis

- **Both >98%:** ✓ No regression
- **Test <Control:** ⚠️  Investigate errors
- **Test >Control:** ✓ Better reliability

## Detailed Test Walkthrough

### Test 1: Explicit Addressing

```
Control (routing off):
  Agent A: Responds to "Agent A, do X" ✓
  Agent B: Responds to "Agent A, do X" ✓
  Agent C: Responds to "Agent A, do X" ✓
  Result: 3 responses, tokens: 3x normal

Test (routing on):
  Agent A: Responds to "Agent A, do X" ✓
  Agent B: SUPPRESSES to "Agent A, do X" ✓
  Agent C: SUPPRESSES to "Agent A, do X" ✓
  Result: 1 response, tokens: 1x normal
  Savings: 66% tokens
```

### Test 2: Broadcast Message

```
Control:
  All agents respond ✓
  
Test:
  All agents respond ✓
  (Broadcast detection correctly identifies for all)
```

### Test 3: Ambiguous Message

```
Control:
  All agents respond ✓
  
Test:
  All agents respond ✓
  (Safe default: ambiguous → respond)
```

## Troubleshooting

### Test Connection Failed

```bash
# Verify proxy is running
curl http://localhost:8801/health

# Check configuration
env | grep DUAL_LOBE

# Restart proxy
docker-compose restart dual-lobe-proxy
```

### Low Pass Rate

```bash
# Check routing logs
grep "recipient_route" /var/log/dual-lobe-proxy.log

# Verify agent names match test scenarios
export DUAL_LOBE_AGENT_NAME="data_analyst"

# Adjust confidence threshold
export DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.5
```

### High Latency

```bash
# Check if LLM provider is slow
curl -i https://your-llm-provider/health

# Check proxy resources
top -p $(pgrep -f dual-lobe-proxy)

# Restart proxy if needed
systemctl restart dual-lobe-proxy
```

### Token Usage Different Than Expected

```bash
# Verify routing is enabled
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Check suppression rate
grep "response_suppressed" /var/log/dual-lobe-proxy.log | wc -l

# Adjust confidence threshold if too conservative
export DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.6
```

## Advanced Testing

### Custom Scenarios

Edit `SCENARIOS` list in `test_recipient_routing_live.py`:

```python
SCENARIOS = [
    {
        "name": "Your Custom Test",
        "agent": "agent_name",
        "message": "Your message here",
        "should_respond": True,  # or False
        "expected_confidence_min": 0.7,
    },
    # Add more scenarios
]
```

### Load Testing

```bash
# Simulate 100 concurrent users
python test_recipient_routing_benchmark.py --agents 10 --requests 100

# Sustained load test (30 seconds)
for i in {1..30}; do
  python test_recipient_routing_benchmark.py --agents 5 --requests 20
  sleep 1
done
```

### Regression Testing

```bash
# Compare before/after threshold change
export DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.6
python test_recipient_routing_live.py --mode compare > results_threshold_60.txt

export DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.75
python test_recipient_routing_live.py --mode compare > results_threshold_75.txt

# Compare results files
diff results_threshold_60.txt results_threshold_75.txt
```

## CI/CD Integration

### GitHub Actions Example

```yaml
name: Recipient Routing Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    services:
      dual-lobe:
        image: dual-lobe-proxy:latest
        ports:
          - 8801:8801
    
    steps:
      - uses: actions/checkout@v2
      - uses: actions/setup-python@v2
        with:
          python-version: '3.11'
      
      - name: Install dependencies
        run: pip install httpx
      
      - name: Wait for proxy
        run: sleep 10
      
      - name: Run functional tests
        run: python tests/test_recipient_routing_live.py --mode compare
      
      - name: Run performance tests
        run: python tests/test_recipient_routing_benchmark.py --agents 3 --requests 20
```

## Success Criteria

✓ **Functional Testing**
- Pass rate 100% (control and test)
- All 10 scenarios passing
- Events logged correctly

✓ **Performance Testing**
- Latency delta <100ms
- Token savings >15%
- Success rate >98%
- Throughput delta <5%

✓ **Reliability**
- No connection failures
- No silent errors
- Consistent results across runs

## Reporting

Results are automatically saved to JSON:
```bash
test_results_1695052800.json
```

**Contents:**
- Control suite results
- Test suite results
- Scenario-by-scenario comparison
- Calculated metrics (savings, deltas)

## Next Steps After Testing

1. **If all tests pass:**
   - Feature is production-ready
   - Deploy to staging
   - Monitor for 24-48 hours
   - Deploy to production

2. **If some tests fail:**
   - Review specific failing scenarios
   - Check confidence thresholds
   - Adjust agent names if needed
   - Re-run tests

3. **If performance is poor:**
   - Check LLM provider latency
   - Increase confidence threshold (less analysis)
   - Check system resources
   - Contact support if persistent

## Support

For test issues or questions:
- Check DEPLOYMENT_GUIDE.md for configuration
- Review RECIPIENT_ROUTING.md for logic
- Check logs in /var/log/dual-lobe-proxy.log
- Consult TEST_SCENARIOS.md in main docs
