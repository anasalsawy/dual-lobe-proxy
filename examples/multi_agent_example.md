# Multi-Agent Recipient Routing Example

## Setup

Three agents in the same conversation space with recipient routing enabled:

```bash
# Agent 1: Data Analyst
export DUAL_LOBE_AGENT_NAME="data_analyst"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Agent 2: Backend Developer
export DUAL_LOBE_AGENT_NAME="backend_agent"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Agent 3: UI Developer
export DUAL_LOBE_AGENT_NAME="ui_developer"
export DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true
```

## Conversation Flow

### Turn 1: Explicit Assignment

**User:** "data_analyst, can you summarize the Q3 metrics?"

**Routing:**
- `data_analyst`: Detects explicit mention → **RESPONDS** with summary
- `backend_agent`: Detects message for data_analyst → **SUPPRESSES** (but ingests into memory)
- `ui_developer`: Detects message for data_analyst → **SUPPRESSES** (but ingests into memory)

**Result:** Only data_analyst responds. Others remain silent but aware.

---

### Turn 2: Ambiguous Reference

**User:** "Do you think the API should be optimized?"

**Routing:**
- `data_analyst`: Ambiguous "you" → confidence 0.45 → **RESPONDS** (safe default for own work)
- `backend_agent`: "API" keyword + own scope → confidence 0.78 → **RESPONDS**
- `ui_developer`: "API" not UI scope → confidence 0.25 → **SUPPRESSES**

**Issue:** Both analysts respond (ambiguity problem). Next user clarifies...

---

### Turn 3: Explicit Clarification

**User:** "backend_agent, please optimize the API endpoint for pagination"

**Routing:**
- `backend_agent`: Explicit mention → **RESPONDS** with optimization plan
- `data_analyst`: Message for backend_agent → **SUPPRESSES** (ingests: "backend optimizing API")
- `ui_developer`: Message for backend_agent → **SUPPRESSES**

**Result:** Only backend_agent responds. Others know the optimization is in progress.

---

### Turn 4: Status Question

**User:** "Who's working on what right now?"

**Routing:**
- `ui_developer`: Broad question, asking current state → confidence 0.70 → **RESPONDS**
- `backend_agent`: Question isn't direct assignment → **SUPPRESSES**
- `data_analyst`: Question isn't direct assignment → **SUPPRESSES**

**ui_developer has context memory:**
- "data_analyst assigned Q3 metrics" (Turn 1, suppressed but remembered)
- "backend_agent optimizing API" (Turn 3, suppressed but remembered)

**Result:** ui_developer can provide comprehensive status leveraging suppressed context.

---

## Key Principles

### 1. **Block Response**
When recipient routing says "not for you", no output is generated.
- Prevents unwanted responses
- Saves tokens
- Reduces noise

### 2. **Preserve Memory**
Even suppressed messages are ingested into agent context.
- Agent remains aware of all conversations
- Can reference suppressed messages in future turns
- Context accumulates across the entire conversation

### 3. **Safe Defaults**
When recipient is ambiguous, the agent responds.
- Errs on the side of engagement
- Prevents silent failures
- Ambiguous messages still go into memory

### 4. **Transparent Logging**
Every routing decision is logged with metadata.
- Audit trail of suppression events
- Identify problematic ambiguities
- Tune confidence thresholds

---

## Event Examples

### Suppressed Response Event

```json
{
  "kind": "response_suppressed_recipient_routing",
  "actor": "lobe-b",
  "payload": {
    "confidence": 0.92,
    "reasoning": "message explicitly addressed to 'data_analyst', not backend_agent",
    "speaker": "user",
    "detected_recipients": ["data_analyst"]
  }
}
```

### Recipient Routed Event (when response IS generated)

```json
{
  "kind": "recipient_routed",
  "actor": "lobe-b.router",
  "payload": {
    "agent_name": "backend_agent",
    "should_respond": true,
    "confidence": 0.88,
    "reasoning": "message explicitly mentions backend_agent",
    "speaker": "user",
    "detected_recipients": ["backend_agent"]
  }
}
```

---

## Configuration Tuning

### Confidence Threshold

The default is `0.6`. Adjust based on your multi-agent setup:

```bash
# Conservative (0.3): Suppress more, avoid overlap
DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.3

# Balanced (0.6): Good mix of suppression and responsiveness
DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.6

# Liberal (0.85): Suppress less, catch more ambiguous cases
DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.85
```

Look at event logs to find the sweet spot:
- Too many suppressed messages? → Raise the threshold
- Still hearing unintended responses? → Lower the threshold

---

## API Integration

When using the proxy API, suppressed responses appear as:

```python
response = await client.chat.completions.create(
    messages=[...],
    model="...",
    stream=True
)

# Check if response was suppressed
if response.get("suppressed") == "recipient_routing":
    print("Message was for another agent")
    print("Context ingested to memory:", response.get("ingested_to_memory"))
else:
    # Normal response from this agent
    print("Response:", response["choices"][0]["message"]["content"])
```

---

## Best Practices

1. **Use explicit agent names** — "Agent A, please..." rather than ambiguous "please..."
2. **Monitor suppression events** — Check logs to validate routing accuracy
3. **Tune per your team** — Start at 0.6, adjust based on observed patterns
4. **Leverage memory** — Suppressed context is still available; agents can reference it
5. **Test with your agents** — Routing confidence may vary based on agent names and domain

---

## Troubleshooting

### Too many unintended responses
- Lower the `CONFIDENCE_THRESHOLD`
- Use more explicit agent addressing in prompts
- Check `recipient_routed` events to see what confidence levels are assigned

### Legitimate messages being suppressed
- Raise the `CONFIDENCE_THRESHOLD`
- Include agent name/role in messages
- Check for naming conflicts (e.g., "Agent" as a generic term)

### High memory usage
- Recipient routing itself is lightweight (200 token budget)
- Memory grows with conversation length, not suppression count
- Use `context_memory_ttl_seconds` to control retention

---

For detailed documentation, see [RECIPIENT_ROUTING.md](../docs/RECIPIENT_ROUTING.md).
