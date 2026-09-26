#!/usr/bin/env python3
"""Build the "Video edit and publish" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. It uses only the
public API — the same calls the console makes when you click through the builder — so every
line corresponds to something you could do by hand. README.md does exactly that.

WHAT IT BUILDS, AND WHY IT IS THIS SHORT NOW
--------------------------------------------
    fetch-link  →  edit  →  publish
    video_editor   video_editor   media_publisher     ← the agent for each step
    auto           auto           NEEDS A PERSON

The middle step used to be two — "plan the edit" and "apply the plan" — bound to a pair of
skills with a fixed list of operations between them. That shape does not survive contact with
customers: the moment somebody asks for an ending slide, the operation list needs a new word for
it, and the next person asks for a fade. A framework whose answer to every request is a release
is not a framework.

So `edit` binds NOTHING. It carries your instructions, and `video_editor` does the work with the
skills it holds — trim, cut, reframe, caption, overlay, watermark, bookend, and whatever else you
give it later. Add a skill to the agent and the same workflow can do a new thing, with no step
changed and no release. THAT is the point of this example.

VIBE EDITING, LITERALLY
-----------------------
    "Keep 0:28 to 1:48, make it portrait for phones, and put the ending slide on the end."

goes in the instructions box. The agent reads it, picks its own operations and their order, and
records what it did. If it needs something it was not given, it says so rather than guessing.

IT IS SAFE TO RE-RUN. Steps, agents and the workflow are matched by name and reused.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import (  # noqa: E402
    agent_step, connect, define_steps, enable_skills, from_input, from_step, grant, parser,
    publish, step,
)

NAME = "Video edit and publish"
DESCRIPTION = (
    "Post a video link and say what you want done to it in plain English. A video agent does the "
    "editing with the skills it holds; a publishing agent waits for you before it uploads."
)

#: EVERY SKILL THE EDITING AGENT MAY REACH FOR. Not steps — the agent composes these itself, in
#: whatever order the instruction needs. Adding one here (or on the Agents page later) widens what
#: the same workflow can do.
EDITING_SKILLS = [
    "media.fetch_link",     # a link becomes a video BOSS holds
    "media.fetch_image",    # a link becomes a picture: an ending slide, a title card, a logo
    "video.trim",           # keep one range
    "video.cut",            # take ranges out of the middle
    "video.reframe",        # portrait, square, landscape
    "video.caption",        # burn text on
    "video.overlay_image",  # put a picture on screen for a while
    "video.watermark",      # a logo in a corner
    "video.bookend",        # a title card before, an ending slide after
]

#: The two steps that still bind a skill, because both are single, exact acts with nothing to
#: decide: turn this link into a video, and upload this file to that channel.
STEPS = [
    {"label": "Fetch the video", "skill_id": "media.fetch_link",
     "summary": "Turn the pasted link into a video BOSS holds.",
     "inputs": [{"name": "url", "required": True}],
     "agent": "video_editor"},
    {"label": "Publish to YouTube", "skill_id": "publish_youtube",
     "summary": "Upload the approved video as private.",
     # TITLE IS REQUIRED BY THE SKILL. Leaving it off the step meant the run reached the last
     # gate, a person approved it, and only then did it fail for a field nobody was asked for.
     "inputs": [{"name": "media_artifact_id", "required": True},
                {"name": "title", "required": True},
                {"name": "description", "required": False}],
     "agent": "media_publisher"},
]


def main() -> None:
    args = parser("Build the video editing workflow.").parse_args()
    api = connect(args, NAME)

    # The editing agent needs every skill it might reach for, and it needs to HOLD them —
    # `role_skills` is the grant the runtime checks, and a skill an agent was merely told about
    # in its spec is one it will be refused at the first call.
    enable_skills(api, EDITING_SKILLS + [s["skill_id"] for s in STEPS], args)
    grant(api, "video_editor", EDITING_SKILLS, args)
    grant(api, "media_publisher", ["publish_youtube"], args)

    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "fetch-link",
        "steps": [
            step("fetch-link", keys["Fetch the video"],
                 {"url": from_input("video_url")},
                 ["edit"], agent="video_editor", args=args),

            # THE STEP THAT BINDS NOTHING.
            #
            # It gets your sentence and the video the step before it produced. What it does with
            # them is the agent's decision, made from the skills it holds — so "trim it" and
            # "trim it, make it portrait and put a slide on the end" are the same step.
            agent_step("edit", from_input("instructions"), ["publish"],
                       agent="video_editor", args=args,
                       context={
                           "media_artifact_id": from_step("fetch-link", "media_artifact_id"),
                           # PASSED THROUGH AS A FACT, not interpreted here. If the operator gave
                           # a link to an ending slide, the agent has `media.fetch_image` and
                           # will use it; if they did not, this is simply absent.
                           "slide_url": from_input("slide_url"),
                       }),

            # No separate review step: publish_youtube is risk tier 3, and BOSS forces a manual
            # approval at or above the floor whatever a workflow asks.
            step("publish", keys["Publish to YouTube"],
                 {"media_artifact_id": from_step("edit", "media_artifact_id"),
                  "title": from_input("title"),
                  "description": from_input("description")},
                 [], agent="media_publisher", approval="manual", args=args),
        ],
        # WHAT THE START FORM ASKS FOR. Without this the console has nothing to draw and the
        # workflow cannot be started from the UI at all.
        "input_schema": {
            "required": ["video_url", "instructions", "title"],
            "properties": {
                "video_url": {
                    "title": "Video link",
                    "description": "Google Drive share link, or any direct HTTPS link to the "
                                   "file. A Drive link is not copied — it is already where the "
                                   "editing steps read from.",
                    "examples": ["https://drive.google.com/file/d/FILE_ID/view"],
                },
                "instructions": {
                    "title": "What you want done",
                    "description": "Plain English. The agent picks its own operations and their "
                                   "order from the skills it holds. Timecodes may be seconds, "
                                   "M:SS or HH:MM:SS.",
                    "examples": ["Keep 0:28 to 1:48, make it portrait for phones, and put the "
                                 "ending slide on the end."],
                },
                "slide_url": {
                    "title": "Ending slide, title card or logo",
                    "description": "Optional. A link to a PICTURE, or to a short clip. Mention it "
                                   "in the instructions — \"put the ending slide on the end\" — "
                                   "and the agent will fetch it and use it.",
                    "examples": ["https://drive.google.com/file/d/FILE_ID/view"],
                },
                "title": {
                    "title": "YouTube title",
                    "description": "Required by YouTube. The upload is PRIVATE either way.",
                },
                "description": {
                    "title": "YouTube description",
                    "description": "Optional.",
                },
            },
        },
    })

    print("""  Done.

  Start a run: Delivery → Workflows → "Video edit and publish" → Start one.

    video_url      a link to the video — Google Drive, or any direct HTTPS link
    instructions   what you want, in plain English. For example:
                   "Keep 0:28 to 1:48, make it portrait for phones, and put the
                    ending slide on the end."
    slide_url      optional — a link to the ending slide, if your instructions mention one
    title          the YouTube title

  The editing step has no fixed list of operations. To let it do something new, give
  video_editor another skill on the Agents page — the workflow does not change.

  The last step stops and waits. That is not a setting — publish_youtube is
  risk tier 3, and BOSS requires a person at or above the floor.
""")


if __name__ == "__main__":
    main()
