import Kernel.Operations

/-!
# What the server guarantees

The headline results, stated over the two entry points the shell calls:

* `handleInternal_inv`, `handleExternal_inv`: whatever arrives, at whatever
  time, the document the step commits satisfies `Inv` — every clause of
  `check_invariants`, and the armed timer is the earliest armed deadline.
* `reachable_inv`: so does every document any sequence of steps can reach
  from the empty one.
* `handleExternal_shape`, `handleInternal_shape`: the effects come out in the
  order the crash story rests on — arm the new timer, commit, clear the old
  timer, then send — with exactly one commit, and the armed timer is the one
  the committed document records.
-/

namespace Kernel

theorem linearize_fst (old : Option Int) (d : Doc) : (linearize old d).1 = { d with timerAt := minDeadline d } := rfl

/-- The effects `linearize` produces: at most an arm, the commit, at most a disarm. -/
theorem linearize_snd (old : Option Int) (d : Doc) :
    ∃ arm disarm : List Effect,
      (linearize old d).2 = arm ++ [.setDocument { d with timerAt := minDeadline d }] ++ disarm ∧
      (arm = [] ∨ ∃ n, minDeadline d = some n ∧ old ≠ some n ∧ arm = [.setTimeout n]) ∧
      (disarm = [] ∨ ∃ o, old = some o ∧ old ≠ minDeadline d ∧ disarm = [.delTimeout o]) := by
  refine ⟨_, _, rfl, ?_, ?_⟩
  · cases h : minDeadline d with
    | none => exact Or.inl rfl
    | some n =>
      dsimp only
      split
      · rename_i hne; exact Or.inr ⟨n, rfl, hne, rfl⟩
      · exact Or.inl rfl
  · cases old with
    | none => exact Or.inl rfl
    | some o =>
      dsimp only
      split
      · rename_i hne; exact Or.inr ⟨o, rfl, hne, rfl⟩
      · exact Or.inl rfl

theorem committed_append (l r : List Effect) (d : Doc)
    (hl : ∀ e ∈ l, ∀ d', e ≠ .setDocument d') : committed (l ++ .setDocument d :: r) = some d := by
  unfold committed
  induction l with
  | nil => simp
  | cons e l ih =>
    simp only [List.cons_append, List.findSome?_cons]
    split
    · rename_i d' he
      cases e with
      | setDocument d'' => exact absurd rfl (hl _ (by simp) d'')
      | _ => simp at he
    · exact ih (fun e he => hl e (by simp [he]))

theorem committed_linearize (old : Option Int) (d : Doc) (rest : List Effect) :
    committed ((linearize old d).2 ++ rest) = some { d with timerAt := minDeadline d } := by
  obtain ⟨arm, disarm, h, ha, _⟩ := linearize_snd old d
  rw [h]
  simp only [List.append_assoc, List.singleton_append]
  apply committed_append
  rcases ha with rfl | ⟨n, _, _, rfl⟩ <;> simp

theorem core_timerAt {d : Doc} (h : Core d) (t : Option Int) : Core { d with timerAt := t } :=
  ⟨h.nodup, h.weak, h.agrees, h.refs⟩

theorem inv_linearize {old : Option Int} {d : Doc} (h : Core d) : Inv (linearize old d).1 :=
  ⟨core_timerAt h _, rfl⟩

/-! ## Every committed document -/

theorem handleInternal_inv {d : Doc} {now cfg} (h : Core d) :
    ∃ d', committed (handleInternal d now cfg) = some d' ∧ Inv d' :=
  ⟨_, committed_linearize _ _ _, core_timerAt (core_sweepTx h) _, rfl⟩

theorem handleExternal_inv {d : Doc} {req now cfg} (h : Core d) :
    ∃ d', committed (handleExternal d req now cfg).1 = some d' ∧ Inv d' := by
  unfold handleExternal
  dsimp only
  rw [List.append_assoc]
  have h1 := core_timerAt (core_sweepTx (now := now) (cfg := cfg) h) (minDeadline (sweepTx d now cfg).doc)
  have h2 := core_decide (now := now) (cfg := cfg) (tx := { doc := _ }) h1 req
  exact ⟨_, committed_linearize _ _ _, core_timerAt h2 _, rfl⟩

/-! ## Every reachable document -/

/-- One step of the server: a deadline or a request, at any time. -/
inductive Step (cfg : Cfg) : Doc → Doc → Prop where
  | internal {d d'} (now : Int) : committed (handleInternal d now cfg) = some d' → Step cfg d d'
  | external {d d'} (req : Req) (now : Int) : committed (handleExternal d req now cfg).1 = some d' → Step cfg d d'

/-- Everything the server can commit, starting from nothing. -/
inductive Reachable (cfg : Cfg) : Doc → Prop where
  | empty : Reachable cfg {}
  | step {d d'} : Reachable cfg d → Step cfg d d' → Reachable cfg d'

theorem inv_empty : Inv {} := by
  refine ⟨⟨List.nodup_nil, ?_, ?_, ?_⟩, rfl⟩ <;> intro o ho <;> simp at ho

theorem step_inv {cfg} {d d' : Doc} (h : Inv d) (s : Step cfg d d') : Inv d' := by
  cases s with
  | internal now e =>
    obtain ⟨d'', e', h''⟩ := handleInternal_inv (now := now) (cfg := cfg) h.1
    rw [e] at e'; cases e'; exact h''
  | external req now e =>
    obtain ⟨d'', e', h''⟩ := handleExternal_inv (req := req) (now := now) (cfg := cfg) h.1
    rw [e] at e'; cases e'; exact h''

/-- **The invariants hold of every document the server can ever commit.** -/
theorem reachable_inv {cfg} {d : Doc} (h : Reachable cfg d) : Inv d := by
  induction h with
  | empty => exact inv_empty
  | step _ s ih => exact step_inv ih s

/-! ## The order of effects -/

/-- Arm, commit, disarm, send: one commit, the arm (if any) before it and for
the deadline the committed document records, the disarm (if any) after it
and for the deadline that was armed before, every message last. -/
def Shaped (old : Option Int) (fx : List Effect) : Prop :=
  ∃ (d : Doc) (arm disarm : List Effect) (sends : List Send),
    fx = arm ++ [.setDocument d] ++ disarm ++ sends.map .send ∧
    (arm = [] ∨ ∃ n, d.timerAt = some n ∧ old ≠ some n ∧ arm = [.setTimeout n]) ∧
    (disarm = [] ∨ ∃ o, old = some o ∧ old ≠ d.timerAt ∧ disarm = [.delTimeout o])

theorem handleInternal_shape (d : Doc) (now cfg) : Shaped d.timerAt (handleInternal d now cfg) := by
  obtain ⟨arm, disarm, h, ha, hd⟩ := linearize_snd d.timerAt (sweepTx d now cfg).doc
  exact ⟨_, arm, disarm, _, by unfold handleInternal; dsimp only; rw [h], ha, hd⟩

theorem handleExternal_shape (d : Doc) (req now cfg) : Shaped d.timerAt (handleExternal d req now cfg).1 := by
  unfold handleExternal
  dsimp only
  generalize decide_ _ req now cfg = r
  obtain ⟨arm, disarm, h, ha, hd⟩ := linearize_snd d.timerAt r.1.doc
  refine ⟨{ r.1.doc with timerAt := minDeadline r.1.doc }, arm, disarm,
    (sweepTx d now cfg).sends.filter (stillDue r.1.doc) ++ r.1.sends, ?_, ha, hd⟩
  rw [h, List.append_assoc (arm ++ _ ++ disarm), ← List.map_append]

end Kernel
