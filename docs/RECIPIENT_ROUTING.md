# Multi-Agent Recipient Routing

## Problem Statement

In multi-agent environments where multiple AI agents operate in the same conversation space, every agent receives every message and generates a response by default. This causes agents to "talk over each other" — responding to messages clearly directed at other agents, wasting tokens and creating confusion.

**Example:**
```
User: "Agent A, please summarize the data"
Agent A responds: "Here's the summary..."
Agent B responds: "I also think the summary is..."  ← Unintended
Agent C responds: "And I agree with the summary..."  ← Unintended
```

## Solution: Intelligent Recipient Routing

The dual-lobe-proxy now includes a **recipient router** in Lobe B that:

1. **Analyzes incoming messages** to determine their intended recipient(s)
2. **Blocks response generation** if the message isn't directed at the current agent
3. **Preserves message context** by storing it in memory, so the agent remains aware of everything said
4. **Uses lightweight detection** (low token cost, 200 token budget) that runs on every cycle

### How It Works

```
Message arrives → Lobe B recipient router analyzes
├─ If directed at THIS agent → Generate response (normal flow)
└─ If directed at someone else → Suppress output BUT store in memory
     ↓
     Next turn: agent has context of the suppressed message
     and can reference it if needed
```

### Key Principle: Memory ≠ Response

Critical distinction:
- **Message blocked** = no response output generated
- **Message ingested** = stored in context memory and available for future turns
- **Agent awareness** = even silent messages influence the agent's state and reasoning

## Configuration

Enable and configure recipient routing via environment variables or settings:

```bash
# Enable recipient routing
DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

# Identify this agent
DUAL_LOBE_AGENT_NAME="backend_agent"

# Confidence threshold (0.0-1.0): suppress response only if confidence ≥ this
DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.6
```

Or in code:

```python
from dual_lobe.core.settings import get_settings

settings = get_settings()
settings.recipient_routing_enabled = True
settings.agent_name = "backend_agent"
settings.recipient_routing_confidence_threshold = 0.6
```

## Recipient Detection Rules

The router uses these rules to determine if a message is for this agent:

1. **Explicit mention** — "Agent A, please...", "@backend_agent", "the executor, can you..."
2. **Role reference** — "the backend specialist", "whoever handles database work", "the task runner"
3. **Context inference** — References to prior agent assignments or implied responsibility
4. **Broadcast** — "everyone", "all agents", "team" = directed at all (agent responds)
5. **Ambiguous** — When unclear, defaults to "respond" (safe default)

### Examples

| Message | Intended Recipient | Will Respond? |
|---------|-------------------|---------------|
| "Agent B, please handle this" | Agent B | No (unless B is this agent) |
| "backend service, retry the request" | Backend agent | Yes (if agent name matches) |
| "Can someone check the logs?" | Unspecified; ambiguous | Yes (safe default) |
| "Everyone, summarize your status" | All agents | Yes |
| "Alice, draft the response" | Alice (specific entity) | No (unless agent name is Alice) |

## Event Logging

When recipient routing suppresses a response, Lobe B logs a `response_suppressed_recipient_routing` event:

```json
{
  "kind": "response_suppressed_recipient_routing",
  "payload": {
    "confidence": 0.95,
    "reasoning": "message explicitly addressed to 'Agent A', not current agent",
    "speaker": "user",
    "detected_recipients": ["Agent A", "Agent B"]
  }
}
```

This allows you to:
- Audit suppression decisions
- Tune confidence thresholds based on false positives/negatives
- Understand multi-agent flow in conversation history

## Performance Impact

- **Light**: Router uses only 200 max output tokens (vs. 1400 for full review)
- **Off critical path**: Routing check happens before main observer cycle
- **Cacheable**: Confidence scores allow batch-checking of common patterns
- **Fallback-safe**: If routing analysis fails, agent defaults to responding

## Integration with Lobe B Memory

The recipient router integrates seamlessly with Lobe B's context memory:

1. **All messages ingested** — even suppressed ones go into memory
2. **Memory influences next turn** — the agent's background reasoning uses all context
3. **Concerns recorded** — if the suppressed message contradicts prior output, that's still noted
4. **Silent but aware** — the agent doesn't respond, but it knows what was said

### Example Scenario

```
Turn 1: User says "Agent A, analyze the data"
  → This agent suppresses response (not Agent A)
  → Message stored: "Agent A, analyze the data"

Turn 2: Agent A responds with analysis

Turn 3: User says to this agent: "Do you agree with Agent A?"
  → Agent has CONTEXT from Turn 1 (Agent A was assigned analysis)
  → Can reference Agent A's output when replying
```

## Tuning the Confidence Threshold

The `recipient_routing_confidence_threshold` (default 0.6) controls false positives/negatives:

- **Lower (0.3)**: More suppression; avoids "talking over each other" but risks missing ambiguous messages
- **Higher (0.9)**: Less suppression; catches ambiguous/unclear messages but may still generate unintended responses

**Recommendation**: Start at 0.6 and tune based on event logs. If you see many suppressed messages that should have gotten responses, raise it. If agents are still responding unintentionally, lower it.

## Disabling for Specific Cases

To temporarily allow all responses without recipient routing:

```bash
DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=false
```

Or check routing results programmatically and override for critical messages:

```python
should_respond, analysis = await route_message(...)
if critical_context:
    should_respond = True  # Force response despite routing
```

## API Integration

When using the dual-lobe-proxy API, suppressed responses appear as:

```json
{
  "ok": true,
  "suppressed": "recipient_routing",
  "ingested_to_memory": true
}
```

Consumers can detect suppressed responses by checking the `suppressed` field. The context is still available for downstream agents or future turns via memory.

## Future Enhancements

Possible expansions (not yet implemented):

1. **Multi-language recipient detection** — understand agent names in Chinese, Spanish, etc.
2. **Learned routing patterns** — adjust confidence based on historical accuracy
3. **Team-wide broadcast** — "broadcast mode" for messages meant for everyone
4. **Recipient addressing API** — explicit metadata about intended recipients in API payloads
