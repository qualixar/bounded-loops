# Content Fact Gate: fix the dead citation link

Goal: make `npx --yes markdown-link-check article.md` (run from this
workspace's root, where `article.md` is `seed/article.md`) report every
link alive.

Steps each turn:
  1. Run: `npx --yes markdown-link-check seed/article.md`
  2. If it reports a dead link, read the surrounding sentence and identify
     which claim that citation was supporting.
  3. Replace the dead URL with a real, stable, live citation for the same
     claim. This example points the fabricated "registry policy notes"
     citation at the same live IANA reference already used elsewhere in the
     article (`https://www.iana.org/domains/reserved`) — both sentences are
     in fact about IANA-administered domain-name policy, so the fix is not
     just link-swapping, the citation now genuinely supports the claim it
     is attached to.
  4. Run the check again to confirm.

Done when: `markdown-link-check` reports 0 dead links (exit 0).
Then output: <promise>ALL_LINKS_ALIVE</promise>

Do not delete the citation — replace it with something real.
Do not add new dependencies.
