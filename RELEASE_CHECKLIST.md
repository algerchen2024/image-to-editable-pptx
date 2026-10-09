# Release checklist

- [ ] Confirm the project remains a clean-room public implementation and does not contain non-public/internal skill files.
- [ ] Re-run a simple keyword/path scan for internal markers before publication.
- [ ] Review [NOTICE.md](NOTICE.md), [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), and [ORIGINALITY_AND_PROVENANCE_REPORT.md](ORIGINALITY_AND_PROVENANCE_REPORT.md).
- [ ] Refresh `docs/readme-infographic.png` if the top-level README story changes.
- [ ] Bump `package.json` version and the `V<n>` in `agents/openai.yaml` display name together (validate_skill.py checks they match).
- [ ] Validate the skill structure.
- [ ] Run unit tests.
- [ ] Validate the example PageIR.
- [ ] Compile the example PageIR with the supported PptxGenJS major version.
- [ ] Inspect the generated PPTX structurally.
- [ ] Verify the release bundle contents before tagging.
