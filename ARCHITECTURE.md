================================================================================
DUAL-LOBE PROXY WITH RECIPIENT ROUTING ARCHITECTURE
================================================================================

USER MESSAGE
│ "data_analyst, please summarize the Q3 metrics"
└─────────────────────────────┬──────────────────────────────

Gateway (Lobe A) Handler
│ (Routes message to all agent instances)
├─────Agent Instance 1──┬─────Agent Instance 2──┬─────Agent Instance 3
│     (data_analyst)    │    (backend_agent)    │    (ui_developer)
│     Agent ID: 1       │     Agent ID: 2       │     Agent ID: 3
└──────────┬────────────┴──────────┬────────────┴──────────┬───────

Lobe B: Recipient Router (NEW COMPONENT)
│
├─ Input: message, agent_name
├─ Logic:
│   1. Analyze message intent
│   2. Detect intended recipient(s)
│   3. Calculate confidence score (0.0-1.0)
│   4. Compare to threshold (default 0.6)
│
├─ LLM prompt (200 token budget):
│  "Is this message for '{agent_name}'?"
│
└─ Output: should_respond, confidence, reasoning

           should_respond:          should_respond:
           YES (0.95)              NO (0.92)
           (explicit               (for other agent)
            mention)               (confidence >= 0.6)
             │                      │
             ▼                      ▼
        RESPOND                 SUPPRESS RESPONSE
        (Normal)                (Silent)
        │                       │
        Generate output         Ingest to memory
        "Q3 summary..."         Log event
                                Return empty

             │                      │
             └──────┬───────────────┘
                    ▼
        Response to User:
        │ Agent 1: "Q3 Summary: ..."
        │ Agent 2: (silent - suppressed)
        │ Agent 3: (silent - suppressed)
        └─ "No more agents talking over each other!"

================================================================================
MULTI-TURN FLOW WITH MEMORY PRESERVATION
================================================================================

Turn 1: "data_analyst, summarize Q3 metrics"
  data_analyst: RESPONDS → Output: "Q3 summary..."
  backend_agent: SUPPRESSES → Memory: "data_analyst → Q3 metrics"
  ui_developer: SUPPRESSES → Memory: "data_analyst → Q3 metrics"

Turn 2: "Do you both think we need to optimize the API?"
  data_analyst: RESPONDS (ambiguous) → Output: "Optimization would help..."
  backend_agent: RESPONDS (keyword match) → Output: "I agree, I can..."
  ui_developer: SUPPRESSES → Memory: "API optimization discussed"

Turn 3: "backend_agent, optimize the pagination endpoint"
  backend_agent: RESPONDS → Output: "Optimization plan..."
  data_analyst: SUPPRESSES → Memory: "backend → pagination optimization"
  ui_developer: SUPPRESSES → Memory: "backend → pagination optimization"

Turn 4: "What's the status of ongoing work?"
  ui_developer: RESPONDS (leverages suppressed context)
    Output: "Status:
      - Q3 metrics completed by data_analyst
      - Pagination optimization underway by backend_agent"
  backend_agent: SUPPRESSES
  data_analyst: SUPPRESSES

KEY: Even suppressed messages are in memory and accessible next turn!

================================================================================
SETTINGS & CONFIGURATION
================================================================================

Environment Variables:

  DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true
    Enable/disable recipient routing (default: false)

  DUAL_LOBE_AGENT_NAME="backend_agent"
    Name/role of this agent instance (default: "agent")

  DUAL_LOBE_RECIPIENT_ROUTING_CONFIDENCE_THRESHOLD=0.6
    Minimum confidence to suppress response (default: 0.6)
    Range: 0.0 (suppress all) to 1.0 (suppress none)

Example multi-agent setup:

  Agent 1:
    DUAL_LOBE_AGENT_NAME=data_analyst
    DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

  Agent 2:
    DUAL_LOBE_AGENT_NAME=backend_agent
    DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

  Agent 3:
    DUAL_LOBE_AGENT_NAME=ui_developer
    DUAL_LOBE_RECIPIENT_ROUTING_ENABLED=true

================================================================================
EVENT SCHEMA
================================================================================

When response is suppressed:

  {
    "kind": "response_suppressed_recipient_routing",
    "actor": "lobe-b",
    "payload": {
      "confidence": 0.92,
      "reasoning": "message explicitly addressed to 'data_analyst'",
      "speaker": "user",
      "detected_recipients": ["data_analyst"]
    }
  }

When analyzing recipient (always logged):

  {
    "kind": "recipient_routed",
    "actor": "lobe-b.router",
    "payload": {
      "agent_name": "backend_agent",
      "should_respond": true,
      "confidence": 0.88,
      "reasoning": "message mentions 'backend_agent'",
      "speaker": "user",
      "detected_recipients": ["backend_agent"]
    }
  }

================================================================================
PERFORMANCE CHARACTERISTICS
================================================================================

Token Usage:
  Recipient router: 200 max output tokens (lightweight)
  Full review (Lobe B): 1400 max output tokens
  Ratio: ~14% overhead for routing decision

Latency:
  Recipient analysis: <1s (typically 100-300ms)
  Full review: 2-5s (typical)
  Off critical path: No impact on user response time

Memory Usage:
  Suppressed messages: Stored in context memory (normal)
  Router overhead: Minimal (<1% additional)
  Memory growth: Not increased by suppression count

Token Savings:
  Per suppressed response: 500-1000 tokens saved
  In 10-agent conversation: Up to 50% reduction
  In focused 2-agent conversation: 20-30% reduction

================================================================================
CONFIDENCE THRESHOLD TUNING GUIDE
================================================================================

0.3 (Conservative)
  • High suppression rate, few unintended responses
  • Use when agents have clearly differentiated roles

0.6 (Default/Balanced)
  • Balanced suppression, good for most setups
  • Recommended starting point

0.85 (Liberal)
  • Low suppression rate, more engagement
  • Use when roles overlap significantly

Tuning Process:
  1. Deploy with default (0.6)
  2. Monitor response_suppressed_recipient_routing events
  3. Check for false positives (suppressed when shouldn't be)
  4. Check for false negatives (responded when shouldn't)
  5. Adjust threshold based on findings
  6. Iterate until optimal

================================================================================
