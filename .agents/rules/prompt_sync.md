# Prompt Sync Rule

## Keep `prompt.txt` and the inline fallback in `gemini.py` in sync

Whenever `prompt.txt` is modified, you **must also update** the inline fallback string in
`services/gemini.py` inside the `load_default_prompt()` function to reflect the exact same changes.

The inline fallback is used when `prompt.txt` cannot be read at runtime, so the two must always
be identical in meaning and instructions.

### What to check on every `prompt.txt` edit

1. Open `services/gemini.py` and locate the `load_default_prompt()` function.
2. Find the `# Inline fallback` block (the `return (...)` string).
3. Apply the same change you made to `prompt.txt` to the corresponding line(s) in the inline fallback.
4. Include both files in the same commit.

### Reverse also applies

If the inline fallback in `gemini.py` is modified, propagate the change back to `prompt.txt` as well.
