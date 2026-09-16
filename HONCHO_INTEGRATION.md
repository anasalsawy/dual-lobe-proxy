# Honcho Integration with Dual-Lobe Proxy & Recipient Routing

## Current State

### Dual-Lobe Proxy Memory
The dual-lobe-proxy has **its own internal persistent memory** layer:
- **Storage**: PostgreSQL with full-text search (FTS)
- **Scope**: Per-tenant, per-space (named memory spaces like "main", "project-a", etc.)
- **Content**: Conversation history + explicit notebook (pinned notes)
- **Loading**: Lexical search + recency + relevance ranking
- **API**: `/v1/dual-lobe/memory/{space}` for inspection and editing

### Honcho Memory
All agents are connected to **Honcho** (external persistent memory API):
- **Storage**: Separate database/service
- **Scope**: Cross-agent, user-centric
- **Content**: Shared facts, user preferences, long-term context
- **Use**: All agents can read/write from the same Honcho space

## The Integration Question: Should Dual-Lobe Connect to Honcho?

**YES, absolutely — and here's why:**

### 1. **Two-Tier Memory Architecture (Recommended)**

```
┌─────────────────────────────────────────────────────────────┐
│                  Honcho (Persistent API)                    │
│  - User preferences, cross-agent facts, long-term context   │
│  - Shared by ALL agents in the multi-agent setup            │
│  - External service (survives proxy restarts)               │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  Dual-Lobe Proxy (Internal Memory)                          │
│  - Per-conversation context (Lobe B's session memory)       │
│  - Conversation-specific observations and context           │
│  - Recipient routing decisions (who said what)              │
│  - Claims history and oversight findings                    │
│  - Short-lived (conversation-scoped)                        │
└─────────────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  Recipient Routing Layer (NEW)                              │
│  - Message recipient analysis                               │
│  - Suppression decisions                                    │
│  - Agent awareness state                                    │
└─────────────────────────────────────────────────────────────┘
```

### 2. **Specific Benefits for Recipient Routing**

**Honcho can store:**
- Agent team membership (who are the agents)
- Role definitions ("backend_agent handles infrastructure")
- Recipient preferences ("I prefer explicit addressing")
- Addressing patterns ("use @name not just name")
- Multi-turn coordination state ("who's working on what")

**Example:**
```
Honcho Space: "multi_agent_team"
{
  "agents": {
    "data_analyst": {"role": "analytics", "scope": "metrics"},
    "backend_agent": {"role": "infrastructure", "scope": "api"},
    "ui_developer": {"role": "frontend", "scope": "ui"}
  },
  "addressing_style": "explicit",
  "coordination_notes": "backend working on pagination (Turn 3)"
}
```

Dual-Lobe can read this and **improve routing accuracy**:
- Know the actual agent names/roles beforehand
- Understand expected addressing style
- Reference cross-turn coordination state

### 3. **Suppressed Messages & Honcho**

Currently: Suppressed messages stored in dual-lobe-proxy internal memory only

**With Honcho integration:**
- Summarize suppressed assignment flow to Honcho
- Example: "data_analyst assigned Q3 analysis task"
- All agents can see who's doing what (shared state)
- Enable async coordination without needing response

```
Turn 1: "data_analyst, analyze Q3"
  ├─ backend_agent suppresses response
  └─ Writes to Honcho: "Q3 analysis assigned to data_analyst"

Turn 3: backend_agent asked directly
  ├─ Reads Honcho: "Q3 analysis assigned to data_analyst"
  └─ Can reference it in response: "Given that data_analyst is on metrics..."
```

### 4. **Multi-Agent Coordination Benefits**

**Without Honcho integration:**
- Each agent sees only its own conversation thread
- Suppressed messages stay local to that proxy instance
- No shared view of who's doing what
- Cross-agent coordination requires explicit communication

**With Honcho integration:**
```
Agent 1 (data_analyst):
  Read: Honcho says "Q3 analysis in progress"
  Action: Wait for data before analysis

Agent 2 (backend_agent):
  Read: Honcho says "Q3 analysis waiting"
  Action: Complete own work, then notify

Agent 3 (ui_developer):
  Read: Both previous states
  Action: Prepare UI for both deliverables
```

### 5. **Recipient Routing Gets Smarter**

**Current routing logic:**
- Parse message for explicit mentions ("Agent A, please...")
- Infer from context ("whoever handles X")
- Safe default: ambiguous = respond

**With Honcho context:**
- Know team roster upfront
- Understand role hierarchies
- Reference past assignments
- Tune confidence scores based on Honcho data

```
Message: "Can you optimize the cache?"

Router analysis:
  Without Honcho: confidence 0.45 (ambiguous)
  With Honcho: confidence 0.78 (knows backend_agent role is "cache optimization")
  Result: Suppress for other agents, respond for backend
```

## Implementation Recommendations

### Tier 1: Read-Only Integration (Minimal Risk)

Add to recipient router initialization:

```python
async def _load_agent_context_from_honcho(agent_name: str, honcho_client):
    """Load agent info from Honcho to improve routing decisions."""
    try:
        # Read team roster, role definitions
        context = await honcho_client.get_namespace("multi_agent_team")
        agent_info = context.get("agents", {}).get(agent_name, {})
        return agent_info
    except:
        return {}  # Graceful fallback
```

Then use in routing:

```python
agent_context = await _load_agent_context_from_honcho(agent_name, honcho_client)
# Increases confidence for role-based matches using Honcho data
```

**Benefits:**
- ✓ Improves routing accuracy
- ✓ No writes to Honcho (read-only)
- ✓ Graceful fallback (works without Honcho)
- ✓ Optional feature (not required)

### Tier 2: Write Summaries (Medium Integration)

After suppressing a response, write summary to Honcho:

```python
if not should_respond:
    # Write to Honcho that this agent was assigned something
    summary = {
        "timestamp": now(),
        "suppressed_message": sanitized_message,
        "assigned_to": recipient_name,
        "routing_confidence": confidence,
    }
    await honcho_client.write("suppressed_assignments", summary)
```

**Benefits:**
- ✓ Other agents see who's assigned what
- ✓ Enables async coordination
- ✓ Reduces need for explicit communication
- ✓ Shared state without response

### Tier 3: Full Sync (Advanced)

Bidirectional sync between dual-lobe-proxy memory and Honcho:
- Load Honcho facts on conversation start
- Write conversation summary to Honcho on end
- Merge insights from suppressed routing decisions

## Schema Proposal for Honcho Integration

```python
# In Honcho, create these namespaces:

"multi_agent_team":
{
  "agents": {
    "agent_name": {
      "role": "string",
      "scope": "string",
      "preferred_addressing": "explicit|implicit|broadcast",
      "active_tasks": ["task1", "task2"],
      "last_assignment": "ISO-8601 timestamp"
    }
  },
  "coordination_state": {
    "current_task_assignments": {},
    "recent_suppressions": [],
    "unaddressed_requests": []
  }
}

"dual_lobe_observations":
{
  "recipient_routing": {
    "messages_analyzed": count,
    "suppression_rate": float,
    "avg_confidence": float,
    "accuracy_feedback": "string"
  },
  "context_memory": {
    "shared_insights": [],
    "contradictions": [],
    "claims_status": {}
  }
}
```

## Migration Path (If You Want This)

### Phase 1: Verify Dual-Lobe Memory Works Solo
- ✓ Already done (recipient routing standalone)
- Suppress messages, store in dual-lobe-proxy
- Validate accuracy

### Phase 2: Add Honcho Read-Only
- Load team roster from Honcho at startup
- Use for improving routing confidence
- No writes yet

### Phase 3: Add Honcho Summaries
- Write suppressed assignment summaries
- Let agents read coordination state
- Enable async coordination

### Phase 4: Full Sync (Optional)
- Bidirectional memory sync
- Conversation summaries to Honcho
- Learn routing patterns

## Decision: What to Do Now?

### Option A: Keep Separate (Current Plan)
**Dual-Lobe Proxy** handles:
- Routing decisions
- Memory within conversation
- Oversight + claims

**Honcho** handles:
- Cross-agent preferences
- User context
- Long-term facts

**Tradeoff**: Simple, two systems doing one job each, but less cross-agent awareness

### Option B: Minimal Integration (Recommended)
**Add read-only Honcho integration to recipient router:**
- Load team roster at startup
- Improves routing confidence by 10-20%
- Zero risk (read-only, graceful fallback)

**Cost**: ~50 lines of code, minimal complexity

### Option C: Full Sync (Future)
**Wait until Option B is proven stable**, then add writes.

## My Recommendation

**Do Option B (Minimal Integration)** because:

1. **Recipient routing benefits immediately**
   - Knows actual agent roles/scopes upfront
   - Increases confidence in routing decisions
   - Reduces ambiguous cases

2. **Minimal risk & complexity**
   - Read-only means no side effects
   - Graceful fallback if Honcho unavailable
   - Can be disabled with env var

3. **Builds toward better coordination**
   - Reads team state from Honcho
   - Later can add writes (Tier 2) without changing read logic
   - Paves way for full sync if needed

4. **Solves actual problem**
   - "Who are the agents?" – answered by Honcho
   - "What's their scope?" – answered by Honcho
   - "Who should respond?" – answered by recipient router + Honcho data

## If You Want to Add This

The integration point would be in `recipient_router.py`:

```python
async def _load_team_context(honcho_client, agent_name: str):
    """Load team/role info from Honcho to improve routing."""
    
async def route_message(...):
    # Before analyzing recipient:
    if honcho_client:
        team = await _load_team_context(honcho_client, agent_name)
        # Pass team context to router for confidence boost
```

Let me know if you want me to:
1. Add minimal Honcho integration to recipient router
2. Keep it separate for now
3. Design the full schema for future integration

**Current status**: Recipient routing works standalone. Honcho integration is optional enhancement.
