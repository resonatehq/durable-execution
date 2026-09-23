import Kernel.Evolution

/-!
# Fencing

A task's version is its fencing token: a claim bumps it, and every operation a
holder performs names the version it holds. The claim this file proves is the
one the design rests on:

**Once a task has moved past version `v`, a request carrying `v` can never
change anything again.** It is refused with a 4xx, and the step it arrives in
commits exactly what a bare sweep at that instant would have committed.

`stale_is_refused` holds from any reachable document, along any run, at any
time. It is the composition of three facts: versions never go down
(`reachable_evolves`), the sweep does not lower them either, and each fenced
operation checks the version before it writes anything (`decide_stale`).
-/

namespace Kernel

open Doc

/-- The requests that act under a fencing token, and the token they carry. -/
def fencedBy : Req → Option (String × Nat)
  | .taskAcquire id v _ _ => some (id, v)
  | .taskRelease id v => some (id, v)
  | .taskFulfill id v _ => some (id, v)
  | .taskSuspend id v _ => some (id, v)
  | .taskFence id v _ _ => some (id, v)
  | _ => none

/-- The task named `id`, if any, is not at version `v`. -/
def NotAt (d : Doc) (id : String) (v : Nat) : Prop :=
  ∀ o, d.get id = some o → ∀ t, o.task = some t → t.version ≠ v

theorem acquiredAt_none {tx : Tx} {id v} (h : NotAt tx.doc id v) : acquiredAt tx id v = none := by
  unfold acquiredAt
  split
  · rename_i o' id' p' t hg
    split
    · rename_i hc
      simp only [Bool.and_eq_true, beq_iff_eq] at hc
      exact absurd hc.2 (h _ hg t rfl)
    · rfl
  · rfl

/-- A fenced request whose token is stale changes nothing and is refused. -/
theorem decide_stale {tx : Tx} {req id v now cfg} (hf : fencedBy req = some (id, v)) (h : NotAt tx.doc id v) :
    (decide_ tx req now cfg).1 = tx ∧ 400 ≤ (decide_ tx req now cfg).2.status := by
  have ha := acquiredAt_none h
  cases req with
  | taskAcquire id' v' pid ttl =>
    simp only [fencedBy, Option.some.injEq, Prod.mk.injEq] at hf
    obtain ⟨rfl, rfl⟩ := hf
    show (taskAcquire tx id' v' pid ttl now cfg).1 = tx ∧ 400 ≤ (taskAcquire tx id' v' pid ttl now cfg).2.status
    unfold taskAcquire
    split
    · exact ⟨rfl, by simp [Reply.err]⟩
    · split
      · rename_i o' id'' p' t hg
        split
        · exact ⟨rfl, by simp [Reply.err]⟩
        · split
          · exact ⟨rfl, by simp [Reply.err]⟩
          · rename_i hv
            exact absurd (by simpa using hv) (h _ hg t rfl)
      · exact ⟨rfl, by simp [Reply.err]⟩
  | taskRelease id' v' =>
    simp only [fencedBy, Option.some.injEq, Prod.mk.injEq] at hf
    obtain ⟨rfl, rfl⟩ := hf
    show (taskRelease tx id' v' now cfg).1 = tx ∧ 400 ≤ (taskRelease tx id' v' now cfg).2.status
    unfold taskRelease
    rw [ha]
    split <;> exact ⟨rfl, by simp [Reply.err]⟩
  | taskFulfill id' v' a =>
    simp only [fencedBy, Option.some.injEq, Prod.mk.injEq] at hf
    obtain ⟨rfl, rfl⟩ := hf
    show (taskFulfill tx id' v' a now cfg).1 = tx ∧ 400 ≤ (taskFulfill tx id' v' a now cfg).2.status
    unfold taskFulfill
    rw [ha]
    split
    · exact ⟨rfl, by simp [Reply.err]⟩
    · split
      · exact ⟨rfl, by simp [Reply.err]⟩
      · split <;> exact ⟨rfl, by simp [Reply.err]⟩
  | taskSuspend id' v' aw =>
    simp only [fencedBy, Option.some.injEq, Prod.mk.injEq] at hf
    obtain ⟨rfl, rfl⟩ := hf
    show (taskSuspend tx id' v' aw cfg).1 = tx ∧ 400 ≤ (taskSuspend tx id' v' aw cfg).2.status
    unfold taskSuspend
    rw [ha]
    repeat' (first | exact ⟨rfl, by simp [Reply.err]⟩ | split)
  | taskFence id' v' c a =>
    simp only [fencedBy, Option.some.injEq, Prod.mk.injEq] at hf
    obtain ⟨rfl, rfl⟩ := hf
    show (taskFence tx id' v' c a now cfg).1 = tx ∧ 400 ≤ (taskFence tx id' v' c a now cfg).2.status
    unfold taskFence
    rw [ha]
    dsimp only
    repeat' (first | exact ⟨rfl, by simp [Reply.err]⟩ | split)
  | _ => simp [fencedBy] at hf

/-- Past `v` in one document, past `v` in every document it evolves into. -/
theorem past_evolves {d d' : Doc} (hn : d'.ids.Nodup) (he : DocEvolves d d') {o t id v}
    (ho : o ∈ d.objects) (hid : o.id = id) (ht : o.task = some t) (hv : v < t.version) : NotAt d' id v := by
  obtain ⟨o', ho', e⟩ := he o ho
  intro x hg tx htx
  have : x = o' := (eq_of_get hn hg ho' (e.id.trans hid)).symm
  subst this
  exact Nat.ne_of_gt (Nat.lt_of_lt_of_le hv (e.version t tx ht htx))

theorem handleExternal_of_decide {d : Doc} {req now cfg}
    (h : (decide_ { doc := { (sweepTx d now cfg).doc with timerAt := minDeadline (sweepTx d now cfg).doc } } req now cfg).1 =
      { doc := { (sweepTx d now cfg).doc with timerAt := minDeadline (sweepTx d now cfg).doc } }) :
    committed (handleExternal d req now cfg).1 = committed (handleInternal d now cfg) := by
  unfold handleExternal handleInternal
  dsimp only
  rw [h, List.append_assoc, committed_linearize, committed_linearize]
  rfl

/-- **A request under a stale fencing token is refused, and the step it
arrives in commits exactly what a bare sweep would.** From any reachable `d`
in which task `id` is already past version `v`, along any run to `d'`, at any
time. -/
theorem stale_is_refused {cfg} {d d' : Doc} {o t id v req now}
    (hr : Reachable cfg d) (ho : o ∈ d.objects) (hid : o.id = id) (ht : o.task = some t) (hv : v < t.version)
    (hs : Steps cfg d d') (hf : fencedBy req = some (id, v)) :
    400 ≤ (handleExternal d' req now cfg).2.status ∧
      committed (handleExternal d' req now cfg).1 = committed (handleInternal d' now cfg) := by
  have hc' : Core d' := (reachable_inv (hs.reachable hr)).1
  have hsw : Core (sweepTx d' now cfg).doc := core_sweepTx hc'
  have he := (reachable_evolves hr hs).trans (evolves_sweepTx d' now cfg)
  have hna : NotAt ({ (sweepTx d' now cfg).doc with timerAt := minDeadline (sweepTx d' now cfg).doc } : Doc) id v :=
    past_evolves (d' := (sweepTx d' now cfg).doc) hsw.nodup he ho hid ht hv
  obtain ⟨h1, h2⟩ := decide_stale (tx := { doc := _ }) (now := now) (cfg := cfg) hf hna
  exact ⟨h2, handleExternal_of_decide h1⟩

/-- The version a successful claim hands out is past every version the task
held before, which is what makes the previous holder's token stale. -/
theorem claim_bumps {o : Obj} {t pid ttl now} (ht : o.task = some t) :
    ∃ t', (claim pid ttl now o).task = some t' ∧ t'.version = t.version + 1 := by
  simp [claim, ht]

end Kernel
