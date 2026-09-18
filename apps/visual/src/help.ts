/** The help overlay. Same explanation the Textual display carried, minus markup. */
export const HELP = `issue-runner — one GitHub issue becomes a chain of small, proven commits.

Keys
  h help on/off      escape close this panel
  ↑ ↓ PageUp PageDown Home End  scroll this panel
  q detach the display — the run keeps going; type r + Enter to reattach
  s save summary     d dismiss summary — on the run summary overlay, however the run ended
  Ctrl+C stop cleanly at the next model call, keeping state and the worktree

Roles and what each prompt is given
  planner        sees the issue title and body only. Splits it into small tickets, each
                 with ONE test assertion and its dependencies. It writes no code.
  builder.tester sees one ticket. Writes a single failing test for that assertion, and
                 nothing else. It may not touch source files.
  builder.coder  sees the ticket and the accepted test. Writes the least code that makes
                 the test pass. The test file is frozen: it cannot edit the test to pass.
  verifier       sees the ticket, the test and the diff, read-only. Answers pass,
                 refine_test or rework_code, with the reason fed back to the right role.

Where the agents' conversation lives
  Every handoff is posted as a comment on that ticket's own sub-issue: the planner's brief,
  the accepted test, the implementation ready for review, the verdict — and every hand-back,
  such as a rejected stub or a failed regression. A ticket that goes right first time is
  documented just as fully as one that takes four rounds. When work IS sent back, the next
  prompt does not repeat the reason; it sends the agent to the issue to read the thread.
  The issue itself is labelled "in progress" the moment the run starts and unlabelled when
  it ends, however it ends. Each sub-issue is labelled the same way as its ticket starts,
  and cleared when the ticket is closed as completed — the last thing that happens before
  the next ticket begins. Status marking is best-effort: a tracker that refuses it never
  stops work, and a mark the runner never managed to set is never taken away.
  With no tracker (--no-github-tickets, --issue-file) the feedback is inlined as before.

Checks and controls
  red first        a new test must fail before any code is written; a test that passes on
                   arrival is handed to the verifier to prove it is not a tautology
  frozen test      the accepted test is hashed; if it changes outside the tester phase the
                   ticket stops
  clean worktree   every run works in its own git worktree and branch, never your checkout
  bounded loops    max_rounds caps verifier hand-backs, then the ticket blocks rather than
                   looping forever
  regression gate  the whole suite must pass on the approved workspace before any commit
  guarded commit   only the approved file set is staged, and a commit hook that alters the
                   tree is rejected
  budget           per-call and per-run AI credit caps pause the run instead of overspending
  resumable        state is saved per ticket, so a stopped run resumes where it left off

Probabilistic generation, deterministic proof
  The model is free to propose: how to split the work, how to phrase a test, how to implement.
  None of that is trusted on its word. Every proposal has to survive something that cannot be
  argued with — a test that must go red then green, a full suite that must stay green, a diff
  that must match the approved file set, a git tree that must match what was verified.
  The AI supplies the judgement, the shell supplies the verdict. When the two disagree, the
  shell wins and the ticket blocks with the reason on the board.`

export function helpText(): string {
  return HELP
}
