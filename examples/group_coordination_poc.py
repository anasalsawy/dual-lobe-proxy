from dual_lobe_crewai.group_coordination import AgentIdentity, GroupCoordinator, GroupMessage

group = GroupCoordinator([
    AgentIdentity("sarah", "Sarah", aliases=("@sarah",)),
    AgentIdentity("david", "David", aliases=("@david",)),
    AgentIdentity("maya", "Maya", aliases=("@maya",)),
])

message = GroupMessage(text="Sarah, check the deployment logs")
deliveries = group.route(message)

# Simulate every model trying to produce text. The transport gate is authoritative.
attempted = {
    "sarah": "I am checking the deployment logs.",
    "david": "I also have a thought.",
    "maya": "I can help too.",
}

for agent_id, delivery in deliveries.items():
    published = group.gate_output(delivery, attempted[agent_id])
    print(
        f"{agent_id}: mode={delivery.mode.value} "
        f"can_emit={delivery.can_emit} "
        f"published={published!r}"
    )

print("\nDavid awareness:")
print(group.consume_awareness_context("david"))
print("\nMaya awareness:")
print(group.consume_awareness_context("maya"))
