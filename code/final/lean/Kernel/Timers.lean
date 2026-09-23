import Kernel.Wakeups

/-!
# Every pending workflow has a timer

A workflow is an origin's document, and its root is the promise whose id is
the origin itself. A root is always external: an internal promise is a step
inside some task, and a workflow is not inside anything. The claim: while a
workflow's root is pending, the document has a timer armed, and it fires no
later than the root's own deadline. It need not be the root's deadline: it is
the earliest deadline anywhere in the workflow.

More generally, every pending external promise has a timer armed at or
before its deadline. That is what makes a durable sleep wake on time: a sleep
is an external timer promise with no target, and until this held, nothing
armed it. The kernel used to arm only targeted promises, so a one-minute
sleep in a workflow with a one-day deadline slept for a day and then timed
out with its root.

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

/-- Any pending external promise puts its deadline among the candidates, so
`minDeadline` exists and is no later. -/
theorem minDeadline_le {d : Doc} {o : Obj} (ho : o ∈ d.objects) (hp : o.promise.state = .pending)
    (he : o.promise.isExternal) : ∃ at_, minDeadline d = some at_ ∧ at_ ≤ o.promise.timeoutAt := by
  apply mem_min
  refine List.mem_flatMap.2 ⟨o, ho, ?_⟩
  simp [objDeadlines, Promise.timeoutArmed, hp, he]

/-- **In every reachable document, every pending external promise has a timer
armed, firing no later than its deadline.** -/
theorem reachable_timer {cfg} {d : Doc} (h : Reachable cfg d) {o : Obj} (ho : o ∈ d.objects)
    (hp : o.promise.state = .pending) (he : o.promise.isExternal) :
    ∃ at_, d.timerAt = some at_ ∧ at_ ≤ o.promise.timeoutAt := by
  rw [(reachable_inv h).2]
  exact minDeadline_le ho hp he

/-- **Every pending workflow has a timer scheduled, no later than its root's
deadline.** The root is external, as every root is. -/
theorem pending_workflow_has_timer {cfg} {d : Doc} (h : Reachable cfg d) {root : Obj}
    (hr : root ∈ d.objects) (_ : IsRoot root) (hp : root.promise.state = .pending)
    (he : root.promise.isExternal) :
    ∃ at_, d.timerAt = some at_ ∧ at_ ≤ root.promise.timeoutAt :=
  reachable_timer h hr hp he

end Kernel
