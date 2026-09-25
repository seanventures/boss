#!/usr/bin/env python3
"""Build the "Video edit and publish" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. It uses only the
public API — the same calls the console makes when you click through the builder — so every
line corresponds to something you could do by hand. README.md does exactly that.

What BOSS *does* ship is the capability this assembles: the video skills, the `video_editor`
and `media_publisher` roles, and ffmpeg in the images. This only wires them into one workflow.

WHAT IT BUILDS
--------------
    fetch-link  →  plan-edit  →  apply-plan  →  publish
    video_editor   video_editor   video_editor   media_publisher   ← the agent for each step
    auto           auto           auto           NEEDS A PERSON

You supply two things per run: the video link, and what you want done to it in plain English.
Everything else is passed from one step to the next.

IT IS SAFE TO RE-RUN. Steps and the workflow are matched by name and reused.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    connect, define_steps, enable_skills, from_input, from_step, parser, publish, step,
)

NAME = "Video edit and publish"
DESCRIPTION = (
    "Post a video link with editing instructions. BOSS fetches it, plans the edit, renders it, "
    "and waits for you before publishing."
)

#: Each binds one skill BOSS ships. `agent` is the role that RUNS the step — and it is not
#: decoration: publish_youtube permits media_publisher and refuses video_editor, so the split
#: below is enforced rather than conventional.
STEPS = [
    {"label": "Fetch the video", "skill_id": "media.fetch_link",
     "summary": "Turn the pasted link into a video BOSS holds.",
     "inputs": [{"name": "url", "required": True}],
     "agent": "video_editor"},
    {"label": "Plan the edit", "skill_id": "video.plan_edit",
     "summary": "Read the instructions and write the edit plan. Renders nothing.",
     "inputs": [{"name": "media_artifact_id", "required": True},
                {"name": "instructions", "required": True}],
     "agent": "video_editor"},
    {"label": "Apply the edit", "skill_id": "video.apply_plan",
     "summary": "Run the plan and record what each operation changed.",
     "inputs": [{"name": "media_artifact_id", "required": True},
                {"name": "plan", "required": True}],
     "agent": "video_editor"},
    {"label": "Publish to YouTube", "skill_id": "publish_youtube",
     "summary": "Upload the approved video as private.",
     "inputs": [{"name": "media_artifact_id", "required": True}],
     "agent": "media_publisher"},
]


def main() -> None:
    args = parser("Build the video editing workflow.").parse_args()
    api = connect(args, NAME)
    enable_skills(api, [s["skill_id"] for s in STEPS], args)
    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "fetch-link",
        "steps": [
            step("fetch-link", keys["Fetch the video"],
                 {"url": from_input("video_url")},
                 ["plan-edit"], agent="video_editor", args=args),
            step("plan-edit", keys["Plan the edit"],
                 {"media_artifact_id": from_step("fetch-link", "media_artifact_id"),
                  "instructions": from_input("instructions")},
                 ["apply-plan"], agent="video_editor", args=args),
            # The video comes from FETCH, not from PLAN: the planner produces a plan, not a
            # video. Wiring it the obvious wrong way is the mistake worth naming.
            step("apply-plan", keys["Apply the edit"],
                 {"media_artifact_id": from_step("fetch-link", "media_artifact_id"),
                  "plan": from_step("plan-edit", "plan")},
                 ["publish"], agent="video_editor", args=args),
            # No separate review step: publish_youtube is risk tier 3, and BOSS forces a manual
            # approval at or above the floor whatever a workflow asks.
            step("publish", keys["Publish to YouTube"],
                 {"media_artifact_id": from_step("apply-plan", "media_artifact_id")},
                 [], agent="media_publisher", approval="manual", args=args),
        ],
    })

    print("""  Done.

  Start a run: Delivery → Workflows → "Video edit and publish" → Start one.

    video_url      a link to the video — Google Drive, or any direct HTTPS link
    instructions   what to do, in plain English. For example:
                   "Cut 2:20 to 2:25 where I fumble, make it vertical for phones,
                    and caption the opening three seconds."

  The last step stops and waits. That is not a setting — publish_youtube is
  risk tier 3, and BOSS requires a person at or above the floor.
""")


if __name__ == "__main__":
    main()
