# Examples

Workflows built from skills BOSS already ships, using only the public API — the same calls the
console makes when you click through the builder.

**None of this is part of BOSS.** Nothing here installs, and nothing runs on a fresh install.
Each example creates data in *your* project, which you can change, rename or delete. What BOSS
ships is the **capability** each one assembles: the skills, the roles, and the tools in the
images. The example is one way to wire them together; yours will differ, which is the point.

Every example has the same shape:

```
examples/<name>/
    README.md    what it does and why, what to set up first, and how to build it BY HAND
    setup.py     the same thing as a script — --dry-run first, safe to re-run
```

---

## Available

### [video-workflow](video-workflow/) — edit a video and publish it

Paste a link, say what you want done in plain English, get an edited video waiting for your
approval before it reaches your channel.

**Steps:** fetch → plan → edit → publish (stops for you)
**Skills:** `media.fetch_link`, `video.plan_edit`, `video.apply_plan`, `publish_youtube`
**Needs:** a connected model, and a Google account for the first and last step

Shows: a model writing a validated plan rather than touching the work; two agents with
different authority; an approval that cannot be switched off.

---

## Candidates

Sketches, not built. Each says honestly what exists and what does not — the difference between
"a day of wiring" and "a new connector with its own OAuth app and platform review" is the
thing worth knowing before picking one.

### Everything needed already ships

**Clip factory — one recording, several shorts.**
Give it a long video and a list of moments; it produces a vertical short for each, captioned
and watermarked, all waiting for one approval.
*Uses:* `media.fetch_link`, `video.plan_edit`, `video.apply_plan`, `publish_youtube`.
*New work:* none in skills — it needs the workflow to repeat a step per clip, which today
means one step per clip or one plan covering them all.
*Worth validating:* is one approval for six shorts right, or one each?

**Release announcement.**
When a deploy succeeds, draft the note, hold it for approval, then post it to the team.
*Uses:* `model.generate_text`, `slack.post_real_guarded` or `mattermost.post_real`, `email.send_real_guarded`.
*New work:* none. All three are risk tier 2, which is the approval floor, so a workflow step
running any of them stops for a person whatever the workflow asks. Slack and email also have
`_guarded` variants that require approval in their own policy; Mattermost has no guarded
variant, so the tier is the only thing holding it — worth knowing before you rely on one.
*Worth validating:* who approves a post that goes to a public channel?

**Weekly digest.**
Gather what shipped, what is waiting and what is over budget; write it; email it.
*Uses:* `model.generate_text`, `email.send_real_guarded`, plus BOSS's own read skills.
*New work:* a schedule trigger — workflows start on demand today.
*Worth validating:* is a digest more useful pushed weekly, or pulled when asked?

**Podcast to clips.**
An audio or video episode in, captioned vertical clips out, one per topic.
*Uses:* the video skills.
*New work:* finding the topics. Today you say where they are; detecting them needs a
transcript step BOSS does not have.
*Worth validating:* would you rather mark the moments yourself than trust detection?

### Needs a new connector first

BOSS has **no** Facebook, Instagram, LinkedIn, X or TikTok skills. Each is a real piece of
work, and not mainly a coding one:

| Platform | What it takes |
|---|---|
| **Instagram / Facebook** | Meta Graph API, a Business account, an app through App Review. Posting is a two-step publish — container, then publish — and video has its own limits |
| **LinkedIn** | Community Management API, partner access for organisation posting |
| **X / Twitter** | API access is paid and tiered; media upload is a separate endpoint |
| **TikTok** | Content Posting API, app review, and an audited scope for direct posting |

The BOSS side is the smaller half: a provider entry, an OAuth flow, one `*.post` skill, a role
that may call it. The platform side — an app, review, and a business account — is what decides
how long it takes.

**If you want one of these, the useful question is which single platform earns it first.** The
same editing lane feeds all of them; only the last step differs.

### Worth considering before any of that

**Scheduled publishing.** Publish at a time, not on approval. Needs a schedule trigger, which
is the same missing piece as the weekly digest — and it makes every publishing example better.

**Cross-posting.** One approval, several destinations. Cheap *once* a second destination
exists, and pointless before.

---

## Which to build next

My reading, for what it is worth:

1. **Clip factory** — it needs nothing new, and it is the video lane earning its keep on a
   real week's work rather than one file.
2. **A schedule trigger** — not an example, a gap. Three of the sketches above want it.
3. **One social connector**, chosen by where you actually publish. Instagram is the most
   asked-for and the most work; LinkedIn is the least work if you post as a company.

Tell me which of these you would actually use and I will build that one properly rather than
guess.
