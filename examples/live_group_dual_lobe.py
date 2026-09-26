"""Interactive real-LLM group test.

Requires the same provider environment variables used by dual-lobe.
Type messages such as:
  Sarah, check this failure
  David, suggest a fix
  Everyone, status

Only agents granted the floor invoke Dual-Lobe and publish output.
"""

import asyncio

from dual_lobe_crewai.group_coordination import (
    AgentIdentity,
    GroupDualLobeRuntime,
    GroupMessage,
)


async def main():
    runtime = GroupDualLobeRuntime([
        AgentIdentity("sarah", "Sarah", aliases=("@sarah",)),
        AgentIdentity("david", "David", aliases=("@david",)),
        AgentIdentity("maya", "Maya", aliases=("@maya",)),
    ])

    print("Dual-Lobe live group test. Ctrl+C to exit.")
    while True:
        text = await asyncio.to_thread(input, "\nyou> ")
        result = await runtime.process_message(GroupMessage(text=text, sender_id="user"))
        if not result.published:
            print("(no agent had the floor)")
            continue
        for agent_id, answer in result.published.items():
            name = runtime.coordinator.identities[agent_id].display_name
            print(f"\n{name}> {answer}")


if __name__ == "__main__":
    asyncio.run(main())
