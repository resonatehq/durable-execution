import Kernel.Wakeups

/-!
# Every pending workflow has a timer

A workflow is an origin's document, and its root is the promise whose id is
the origin itself. The claim: while a workflow's root is pending, the
document has a timer armed, and it fires no later than the root's own
deadline. It need not be the root's deadline: it is the earliest deadline
anywhere in the workflow, and the root's is one of the candidates.

**This needs the root to have a target.** A promise with no
`resonate:target` has no task and no armed deadline; `kernel.py` lets it
expire lazily, when someone next reads it. `untargeted_root_has_no_timer`
below is a document the server really commits: a pending root, nothing
scheduled. A workflow the SDK starts always has a target, because a target
is what makes it dispatchable.

The statement is about the document: `timerAt` is the deadline the shell was
told to arm. Whether the queue really holds a task for it is the shell's
business, and is exactly the arm-before-commit race in `engine.py`.
-/

namespace Kernel

/-- The workflow's root: the promise whose id is its whole origin. -/
def IsRoot (o : Obj) : Prop := originOf o.id = o.id

theorem mem_min {l : List Int} {x : Int} (hx : x ∈ l) : ∃ m, l.min? = some m ∧ m ≤ x := by
  cases hm : l.min? with
  | none => rw [List.min?_eq_none_iff] at hm; subst hm; cases hx
  | some m => exact ⟨m, rfl, (List.min?_eq_some_iff.1 hm).2 x hx⟩

/-- Any pending promise with a target puts its deadline among the candidates,
so `minDeadline` exists and is no later. -/
theorem minDeadline_le {d : Doc} {o : Obj} (ho : o ∈ d.objects) (hp : o.promise.state = .pending)
    (ht : o.promise.target.isSome) : ∃ at_, minDeadline d = some at_ ∧ at_ ≤ o.promise.timeoutAt := by
  apply mem_min
  refine List.mem_flatMap.2 ⟨o, ho, ?_⟩
  simp [objDeadlines, Promise.timeoutArmed, hp, ht]

/-- **In every reachable document, every pending promise with a target has a
timer armed, firing no later than its deadline.** -/
theorem reachable_timer {cfg} {d : Doc} (h : Reachable cfg d) {o : Obj} (ho : o ∈ d.objects)
    (hp : o.promise.state = .pending) (ht : o.promise.target.isSome) :
    ∃ at_, d.timerAt = some at_ ∧ at_ ≤ o.promise.timeoutAt := by
  rw [(reachable_inv h).2]
  exact minDeadline_le ho hp ht

/-- **Every pending workflow whose root has a target has a timer scheduled,
no later than the root's deadline.** -/
theorem pending_workflow_has_timer {cfg} {d : Doc} (h : Reachable cfg d) {root : Obj}
    (hr : root ∈ d.objects) (_ : IsRoot root) (hp : root.promise.state = .pending)
    (ht : root.promise.target.isSome) :
    ∃ at_, d.timerAt = some at_ ∧ at_ ≤ root.promise.timeoutAt :=
  reachable_timer h hr hp ht

/-- Without a target the claim fails: one `promise.create`, from nothing, commits a
pending root with no timer. -/
theorem untargeted_root_has_no_timer :
    committed (handleExternal {} (.promiseCreate { id := "wf", timeoutAt := 1000 }) 0 {}).1 =
      some { objects := [⟨"wf", { timeoutAt := 1000 }, none⟩], timerAt := none } := by
  decide

end Kernel
