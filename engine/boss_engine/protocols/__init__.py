"""Wire protocols BOSS speaks to the outside (AP-P5, docs/design/AGENT_PLATFORM.md §4.5, §5 Phase 5).

  a2a_types   the A2A v1.0 data model (Task, TaskState, Message, Part, Artifact, events): the ONE
              async-task shape BOSS uses, for the A2A server, the A2A client and async sub-agents
              (AP-P7 shapes `start/check/update/cancel` child runs as these Tasks).
"""
