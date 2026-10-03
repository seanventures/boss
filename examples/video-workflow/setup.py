#!/usr/bin/env python3
"""Build the "Video edit and publish" workflow on a BOSS install.

An EXAMPLE. Nothing here ships with BOSS and nothing runs it on install. It uses only the
public API — the same calls the console makes when you click through the builder — so every
line corresponds to something you could do by hand. README.md does exactly that.

WHAT IT BUILDS, AND WHY IT IS THIS SHORT NOW
--------------------------------------------
    edit  →  publish
    video_editor   media_publisher      ← who runs it
    auto           NEEDS A PERSON

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
    agent_step, connect, define_steps, enable_skills, ensure_agents, from_input, from_step,
    grant, parser, publish, step,
)

#: The agents this example names, and the pack that ships them. core_v1 — the default — has
#: neither, so setup installs this pack when they are missing rather than dying at the grant.
AGENTS = ["video_editor", "media_publisher"]
AGENT_PACK = "media_pack"

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
    "media.read_document",  # a link becomes TEXT: a brief, a title, a description

    "video.trim",           # keep one range
    "video.cut",            # take ranges out of the middle
    "video.reframe",        # portrait, square, landscape
    "video.caption",        # burn text on
    "video.overlay_image",  # put a picture on screen for a while
    "video.watermark",      # a logo in a corner
    "video.bookend",        # a title card before, an ending slide after
]

#: The ONE step that still binds a skill, because it is a single exact act with nothing to
#: decide: upload this file to that channel. Fetching a link is not — the agent holds
#: `media.fetch_link` and decides for itself when to use it.
STEPS = [
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
    # THE AGENTS FIRST. Granting a skill to an agent the project does not have is refused,
    # which is where this example used to stop on every default install.
    ensure_agents(api, AGENTS, AGENT_PACK, args)
    grant(api, "video_editor", EDITING_SKILLS, args)
    grant(api, "media_publisher", ["publish_youtube"], args)

    keys = define_steps(api, STEPS, args)
    if args.dry_run:
        print(f"  WORKFLOW\n    would create {NAME!r}\n")
        return

    publish(api, name=NAME, description=DESCRIPTION, args=args, spec={
        "entry_step": "edit",
        "steps": [
            # ONE STEP, AND IT BINDS NOTHING.
            #
            # There used to be a `fetch-link` step in front of this, wired to a required
            # `video_url` field. It meant the operator had to split what they wanted across two
            # boxes — the link in one, the instruction in the other — and pasting the whole
            # thing into the instruction, which is what anybody would do, failed the start with
            # "this workflow needs 1 input(s) it was not given: video_url".
            #
            # The agent already holds `media.fetch_link`. Fetching a link IS one of its skills,
            # so making it a separate step was BOSS deciding the order of operations for work
            # the agent is supposed to order itself. Say what you want, links included, and it
            # fetches, edits and reports back.
            agent_step("edit", from_input("instructions"), ["publish"],
                       agent="video_editor", args=args,
                       # THE PUBLISHING DETAILS, DECIDED HERE AND WIRED TO THE NEXT STEP.
                       # If the instruction points at a brief, the agent reads it with
                       # `media.read_document` and takes them from there.
                       produces=["title", "description"],
                       context={
                           # OPTIONAL, BOTH OF THEM. A link given here is a fact the agent can
                           # use; a link written into the instruction works just as well.
                           "video_url": from_input("video_url"),
                           "asset_url": from_input("asset_url"),
                       }),

            # No separate review step: publish_youtube is risk tier 3, and BOSS forces a manual
            # approval at or above the floor whatever a workflow asks.
            # THE DETAILS COME FROM THE EDITING AGENT, NOT FROM A SECOND FORM.
            #
            # `title` and `description` were fields the operator had to fill in — having
            # already written both in the brief they linked. The agent holds
            # `media.read_document` now, so it reads them and passes them on; the start form is
            # the fallback for a run that names no brief.
            step("publish", keys["Publish to YouTube"],
                 {"media_artifact_id": from_step("edit", "media_artifact_id"),
                  "title": from_step("edit", "title", override_from_input="title"),
                  "description": from_step("edit", "description",
                                           override_from_input="description")},
                 [], agent="media_publisher", approval="manual", args=args),
        ],
        # WHAT THE START FORM ASKS FOR. Without this the console has nothing to draw and the
        # workflow cannot be started from the UI at all.
        "input_schema": {
            # ONLY WHAT CANNOT BE INFERRED. `video_url` was required and is not any more: the
            # link can be written into the instruction like everything else, and demanding it
            # twice is what blocked a start that had said everything it needed to.
            # JUST THE INSTRUCTION. `title` was required and is not any more: if the brief
            # names one, the agent reads it; if it does not, the agent is told to write one.
            "required": ["instructions"],
            "properties": {
                "video_url": {
                    "title": "Video link (optional — you can put it in the instructions instead)",
                    "description": "Google Drive share link, or any direct HTTPS link. Leave it "
                                   "blank and write the link into your instructions; the agent "
                                   "fetches whatever you point it at either way.",
                    "examples": ["https://drive.google.com/file/d/FILE_ID/view"],
                },
                "instructions": {
                    "title": "What you want done",
                    # A PARAGRAPH, AND THE FORM HAS TO KNOW THAT. `format: textarea` is the
                    # explicit signal; without it the console infers prose from a long example,
                    # which works but is a guess. This field IS the step — it is the whole
                    # specification the agent works from — and typing it into a one-line box
                    # where you cannot see what you wrote is how an instruction becomes a title.
                    "format": "textarea",
                    "description": (
                        "Plain English — say it the way you would to a person. The agent picks "
                        "its own operations and their order from the skills it holds, so you "
                        "describe the RESULT, not the steps.\n"
                        "It can keep a section, cut bits out of the middle, reframe to portrait "
                        "or square, burn on a caption, put a picture on screen, add a logo, and "
                        "join a title card or ending slide.\n"
                        "Timecodes: seconds, M:SS or HH:MM:SS. If your timecodes came off the "
                        "original recording, say so — each operation applies to the result of "
                        "the one before it.\n"
                        "Going landscape to portrait CROPS THE SIDES, so anything in a corner "
                        "of the original — a channel badge, a logo — is cut off. To keep it, "
                        "link the image above and say where it belongs.\n"
                        "If it needs something it was not given, it says so rather than "
                        "guessing."),
                    "examples": [
                        "Keep 3:42 to 4:50. Convert it to portrait for phones, taking the "
                        "centre of the frame. The channel badge is already in the top corner — "
                        "keep it exactly as it is. Put the ending slide on the end."],
                },
                "asset_url": {
                    "title": "A picture or clip to use — ending slide, title card, logo, badge",
                    "description": (
                        "Optional. A link to a PICTURE or a short clip. Say in your instructions "
                        "what it is for and the agent uses it accordingly: \"put the ending "
                        "slide on the end\", \"keep the channel badge in the top-right corner\".\n"
                        "WORTH KNOWING: converting landscape to portrait crops the sides, so a "
                        "logo or badge sitting in a corner of the original is cut off. If you "
                        "want it kept, link the image here and say where it goes — the agent "
                        "fetches it and puts it back."),
                    "examples": ["https://drive.google.com/file/d/FILE_ID/view"],
                },
                "title": {
                    "title": "YouTube title (optional — or say where to find it)",
                    "description": "Leave blank if your instructions point at a brief that "
                                   "contains it. The upload is PRIVATE either way, and YouTube "
                                   "refuses a video with no title, so the agent writes one from "
                                   "the brief if you have not.",
                },
                "description": {
                    "title": "YouTube description (optional — or say where to find it)",
                    "description": "Same: written in the brief, typed here, or left out.",
                },
            },
        },
    })

    print("""  Done.

  Start a run: Workflows → Library → "Video edit and publish" → ⋯ → Start.

    video_url      a link to the video — Google Drive, or any direct HTTPS link
    instructions   what you want, in plain English. For example:
                   "Keep 0:28 to 1:48, make it portrait for phones, and put the
                    ending slide on the end."
    asset_url      optional — a picture to use: ending slide, logo, badge
    title          the YouTube title

  The editing step has no fixed list of operations. To let it do something new, give
  video_editor another skill on the Agents page — the workflow does not change.

  The last step stops and waits. That is not a setting — publish_youtube is
  risk tier 3, and BOSS requires a person at or above the floor.
""")


if __name__ == "__main__":
    main()
