# okres-agent-project

| Project | What it does |
|---|---|
| [`video-pipeline/`](video-pipeline/) | Conductor workflow that encodes a video into several formats in parallel, extracts its dialogue (subtitles or Whisper speech-to-text) and summarizes it with Claude. Quick start: `video-pipeline/setup.sh --run` |

The [Conductor skill](https://github.com/conductor-oss/conductor-skills) is vendored in `.claude/skills/conductor` (pinned in `skills-lock.json`), so Claude Code sessions in this repo can create, run and debug Conductor workflows. Update it with `npx skills update`.
