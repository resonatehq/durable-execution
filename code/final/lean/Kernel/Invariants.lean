import Kernel.Model

/-!
# The invariants

`check_invariants` in `kernel.py`, as propositions, and the proof that every
committed document satisfies them: whatever request arrives, whenever it
arrives, and whatever document it arrives at, provided that document
satisfied them too.

The invariants split three ways.

* `Local o` is about one object alone: a promise is pending exactly when it
  has no `settledAt`; its listeners are unique; a task holds exactly the timer
  its state calls for; a task is fulfilled exactly when its promise has
  settled; an object has a task exactly when its promise has a target.
* `Refs ids o` says every id an object mentions — an awaiter in a callback, an
  awaited promise in a resume — names an object that exists.
* `Core d` is both for every object, and ids that are unique. `Inv d` adds
  that the armed timer is the earliest armed deadline.

A settlement is two writes to one object, and between them that object breaks
the fourth clause of `Local`: its promise has settled, its task has not yet
been fulfilled. The sweep does it to every expiring object at once, then
repairs them one at a time. `Held E d` is `Core` with that clause suspended
for the objects `E` picks, and `trigger_settlement` is exactly what takes one
object out of `E`.
-/

namespace Kernel

/-! ## Statements -/

/-- A task holds exactly the timer its state calls for: `one_timer`. -/
def TaskShape (t : Task) : Prop :=
  match t.state with
  | .pending => t.retryAt.isSome ∧ t.leaseAt = none
  | .acquired => t.leaseAt.isSome ∧ t.retryAt = none
  | _ => t.retryAt = none ∧ t.leaseAt = none

/-- Everything about one object except how its task agrees with its promise. -/
structure Weak (o : Obj) : Prop where
  settledAt : o.promise.state = .pending ↔ o.promise.settledAt = none
  listeners : o.promise.listeners.Nodup
  shape : ∀ t, o.task = some t → TaskShape t
  target : o.task.isSome = o.promise.target.isSome

/-- A task is fulfilled exactly when its promise has settled. -/
def Agrees (o : Obj) : Prop := ∀ t, o.task = some t → (o.promise.state = .pending ↔ t.state ≠ .fulfilled)

def Local (o : Obj) : Prop := Weak o ∧ Agrees o

def Refs (ids : List String) (o : Obj) : Prop :=
  (∀ a ∈ o.promise.callbacks, a ∈ ids) ∧ (∀ t, o.task = some t → ∀ r ∈ t.resumes, r ∈ ids)

/-- `Core`, except that the objects `E` picks may have settled promises whose
tasks are not yet fulfilled. -/
structure Held (E : String → Prop) (d : Doc) : Prop where
  nodup : d.ids.Nodup
  weak : ∀ o ∈ d.objects, Weak o
  agrees : ∀ o ∈ d.objects, ¬ E o.id → Agrees o
  refs : ∀ o ∈ d.objects, Refs d.ids o

def Core (d : Doc) : Prop := Held (fun _ => False) d

def Inv (d : Doc) : Prop := Core d ∧ d.timerAt = minDeadline d

theorem Core.local {d : Doc} (h : Core d) : ∀ o ∈ d.objects, Local o :=
  fun o ho => ⟨h.weak o ho, h.agrees o ho (by simp)⟩

theorem Held.core {E} {d : Doc} (h : Held E d) (hE : ∀ o ∈ d.objects, ¬ E o.id) : Core d :=
  ⟨h.nodup, h.weak, fun o ho _ => h.agrees o ho (hE o ho), h.refs⟩

theorem Held.mono {E E' : String → Prop} {d : Doc} (h : Held E d) (hE : ∀ x, E x → E' x) : Held E' d :=
  ⟨h.nodup, h.weak, fun o ho hn => h.agrees o ho (fun hx => hn (hE _ hx)), h.refs⟩

/-! ## The document: `get`, `modify`, `insert` -/

namespace Doc

theorem get_mem {d : Doc} {id o} (h : d.get id = some o) : o ∈ d.objects ∧ o.id = id := by
  unfold get at h
  exact ⟨List.mem_of_find?_eq_some h, by simpa using List.find?_some h⟩

theorem get_none {d : Doc} {id} (h : d.get id = none) : ∀ o ∈ d.objects, o.id ≠ id := by
  unfold get at h
  intro o ho he
  exact (List.find?_eq_none.1 h) o ho (by simp [he])

theorem mem_ids {d : Doc} {o} (h : o ∈ d.objects) : o.id ∈ d.ids := List.mem_map_of_mem h

theorem get_of_mem {d : Doc} {id} (h : id ∈ d.ids) : ∃ o, d.get id = some o := by
  cases hg : d.get id with
  | some o => exact ⟨o, rfl⟩
  | none =>
    obtain ⟨o, ho, rfl⟩ := List.mem_map.1 h
    exact absurd rfl (get_none hg o ho)

theorem nodup_map_inj {α β} {g : α → β} : ∀ {l : List α}, (l.map g).Nodup →
    ∀ {a b}, a ∈ l → b ∈ l → g a = g b → a = b
  | [], _, _, _, ha, _, _ => absurd ha (List.not_mem_nil)
  | x :: xs, hn, a, b, ha, hb, he => by
    rw [List.map_cons, List.nodup_cons] at hn
    rcases List.mem_cons.1 ha with h1 | h1 <;> rcases List.mem_cons.1 hb with h2 | h2
    · rw [h1, h2]
    · subst h1; exact (hn.1 (by rw [he]; exact List.mem_map_of_mem h2)).elim
    · subst h2; exact (hn.1 (by rw [← he]; exact List.mem_map_of_mem h1)).elim
    · exact nodup_map_inj hn.2 h1 h2 he

/-- With unique ids, the object `get` finds is the only one with its id. -/
theorem eq_of_get {d : Doc} (hn : d.ids.Nodup) {id o o'} (h : d.get id = some o)
    (ho' : o' ∈ d.objects) (hid : o'.id = id) : o' = o := by
  obtain ⟨ho, hoid⟩ := get_mem h
  unfold ids at hn
  exact nodup_map_inj hn ho' ho (by rw [hid, hoid])

@[simp] theorem modify_objects (d : Doc) (id f) :
    (d.modify id f).objects = d.objects.map fun o => if o.id == id then f o else o := rfl

@[simp] theorem modify_timerAt (d : Doc) (id f) : (d.modify id f).timerAt = d.timerAt := rfl

/-- The functions `modify` applies all keep the id: they are record updates
of `promise` and `task`. -/
def IdPres (f : Obj → Obj) : Prop := ∀ o, (f o).id = o.id

theorem ids_modify (d : Doc) (id) {f} (hf : IdPres f) : (d.modify id f).ids = d.ids := by
  simp only [ids, modify_objects, List.map_map]
  congr 1
  funext o
  simp only [Function.comp]
  split <;> simp [hf o]

theorem mem_modify {d : Doc} {id f o'} (h : o' ∈ (d.modify id f).objects) :
    ∃ o ∈ d.objects, o' = if o.id == id then f o else o := by
  simp only [modify_objects, List.mem_map] at h
  obtain ⟨o, ho, e⟩ := h
  exact ⟨o, ho, e.symm⟩

theorem map_modify_of_ne {id : String} {f : Obj → Obj} : ∀ {l : List Obj}, (∀ o ∈ l, o.id ≠ id) →
    l.map (fun o => if o.id == id then f o else o) = l
  | [], _ => rfl
  | x :: xs, h => by
    simp only [List.map_cons, List.cons.injEq]
    exact ⟨by simp [h x (by simp)], map_modify_of_ne (fun o ho => h o (by simp [ho]))⟩

theorem modify_of_get_none {d : Doc} {id f} (h : d.get id = none) : d.modify id f = d := by
  cases d with
  | mk objs timerAt =>
    simp only [modify, Doc.mk.injEq, and_true]
    exact map_modify_of_ne (get_none h)

theorem modify_get_eq {id f} (hf : IdPres f) :
    ((fun x : Obj => x.id == id') ∘ fun o => if o.id == id then f o else o) = (fun x => x.id == id') := by
  funext o; simp only [Function.comp]; by_cases h : o.id = id <;> simp [h, hf o]

theorem get_modify_ne {d : Doc} {id id' f} (hf : IdPres f) (hne : id' ≠ id) :
    (d.modify id f).get id' = d.get id' := by
  simp only [get, modify_objects, List.find?_map, modify_get_eq hf]
  cases h : d.objects.find? (fun x => x.id == id') with
  | none => rfl
  | some o =>
    have := List.find?_some h
    simp only [beq_iff_eq] at this
    simp [this, hne]

theorem get_modify_self {d : Doc} (hn : d.ids.Nodup) {id f o} (hf : IdPres f) (h : d.get id = some o) :
    (d.modify id f).get id = some (f o) := by
  obtain ⟨ho, hoid⟩ := get_mem h
  have hmem : f o ∈ (d.modify id f).objects := by
    simp only [modify_objects, List.mem_map]
    exact ⟨o, ho, by simp [hoid]⟩
  cases hg : (d.modify id f).get id with
  | none => exact absurd (by rw [hf, hoid]) (get_none hg (f o) hmem)
  | some o' =>
    obtain ⟨ho', ho'id⟩ := get_mem hg
    have hn' : (d.modify id f).ids.Nodup := by rwa [ids_modify d id hf]
    exact congrArg some (eq_of_get hn' hg hmem (by rw [hf, hoid])).symm

theorem insertSorted_perm (o : Obj) (l : List Obj) : (insertSorted o l).Perm (o :: l) := by
  induction l with
  | nil => simp [insertSorted]
  | cons x xs ih =>
    unfold insertSorted
    split
    · exact List.Perm.refl _
    · exact (List.Perm.cons x ih).trans (List.Perm.swap o x xs)

theorem mem_insert {d : Doc} {o o'} : o' ∈ (d.insert o).objects ↔ o' = o ∨ o' ∈ d.objects := by
  simp [insert, (insertSorted_perm o d.objects).mem_iff]

theorem ids_insert_perm (d : Doc) (o) : (d.insert o).ids.Perm (o.id :: d.ids) := by
  simpa [ids, insert] using (insertSorted_perm o d.objects).map (·.id)

theorem mem_ids_insert {d : Doc} {o x} : x ∈ (d.insert o).ids ↔ x = o.id ∨ x ∈ d.ids := by
  simp [(ids_insert_perm d o).mem_iff]

end Doc

end Kernel

namespace Kernel

theorem Refs.mono {ids ids' : List String} {o} (h : Refs ids o) (hs : ∀ x ∈ ids, x ∈ ids') : Refs ids' o :=
  ⟨fun a ha => hs a (h.1 a ha), fun t ht r hr => hs r (h.2 t ht r hr)⟩

/-! ## One object at a time -/

section Objects

attribute [local simp] TaskShape Task.disarm Task.armRetry Task.armLease

theorem weak_of_eq {o o' : Obj} (h : Weak o) (hp : o'.promise = o.promise) (ht : o'.task = o.task) : Weak o' := by
  obtain ⟨h1, h2, h3, h4⟩ := h
  exact ⟨by rw [hp]; exact h1, by rw [hp]; exact h2, by rw [ht]; exact h3, by rw [ht, hp]; exact h4⟩

theorem fulfilTask_id : Doc.IdPres fulfilTask := by
  intro o; unfold fulfilTask; split <;> (try split) <;> rfl

theorem fulfilTask_promise (o : Obj) : (fulfilTask o).promise = o.promise := by
  unfold fulfilTask; split <;> (try split) <;> rfl

theorem weak_fulfilTask {o : Obj} (h : Weak o) : Weak (fulfilTask o) := by
  obtain ⟨h1, h2, h3, h4⟩ := h
  unfold fulfilTask
  split
  · rename_i t ht
    split
    · refine ⟨h1, h2, ?_, ?_⟩
      · intro t' e; cases e; simp
      · simp [← h4, ht]
    · exact ⟨h1, h2, h3, h4⟩
  · exact ⟨h1, h2, h3, h4⟩

theorem agrees_fulfilTask {o : Obj} (hs : o.promise.state ≠ .pending) : Agrees (fulfilTask o) := by
  unfold fulfilTask Agrees
  split
  · split
    · intro t e; cases e; simp [hs]
    · rename_i h; intro t e; simp_all
  · intro t e; simp_all

theorem clearCallbacks_id : Doc.IdPres clearCallbacks := fun _ => rfl
theorem clearListeners_id : Doc.IdPres clearListeners := fun _ => rfl

theorem weak_clearCallbacks {o : Obj} (h : Weak o) : Weak (clearCallbacks o) :=
  ⟨h.settledAt, h.listeners, h.shape, h.target⟩

theorem weak_clearListeners {o : Obj} (h : Weak o) : Weak (clearListeners o) :=
  ⟨h.settledAt, List.nodup_nil, h.shape, h.target⟩

theorem agrees_clearCallbacks {o : Obj} (h : Agrees o) : Agrees (clearCallbacks o) := h
theorem agrees_clearListeners {o : Obj} (h : Agrees o) : Agrees (clearListeners o) := h

theorem refs_clearCallbacks {ids} {o : Obj} (h : Refs ids o) : Refs ids (clearCallbacks o) :=
  ⟨fun _ ha => by simp [clearCallbacks] at ha, h.2⟩

theorem refs_clearListeners {ids} {o : Obj} (h : Refs ids o) : Refs ids (clearListeners o) := h

theorem mem_addResume {id x : String} {rs : List String} (h : x ∈ addResume id rs) : x = id ∨ x ∈ rs := by
  unfold addResume at h
  split at h
  · exact Or.inr h
  · simp at h
    rcases h with h | h
    · exact Or.inr h
    · exact Or.inl h

theorem wake_id (id now cfg) : Doc.IdPres (wake id now cfg) := by
  intro o; unfold wake; split <;> (try split) <;> (try split) <;> rfl

theorem wake_promise (id now cfg) (o : Obj) : (wake id now cfg o).promise = o.promise := by
  unfold wake; split <;> (try split) <;> (try split) <;> rfl

theorem weak_wake {id now cfg} {o : Obj} (h : Weak o) : Weak (wake id now cfg o) := by
  obtain ⟨h1, h2, h3, h4⟩ := h
  unfold wake
  split
  · rename_i t ht
    split
    · refine ⟨h1, h2, ?_, by simpa [ht] using h4⟩
      intro t' e; cases e; simp
    · split
      · refine ⟨h1, h2, ?_, by simpa [ht] using h4⟩
        intro t' e; cases e
        simpa using h3 t ht
      · exact ⟨h1, h2, h3, h4⟩
  · exact ⟨h1, h2, h3, h4⟩

theorem agrees_wake {id now cfg} {o : Obj} (h : Agrees o) : Agrees (wake id now cfg o) := by
  unfold wake Agrees
  split
  · rename_i t ht
    have := h t ht
    split
    · rename_i hs
      intro t' e; cases e; simp_all
    · split
      · intro t' e; cases e; simpa using this
      · exact h
  · exact h

theorem refs_wake {ids : List String} {id now cfg} {o : Obj} (h : Refs ids o) (hid : id ∈ ids) :
    Refs ids (wake id now cfg o) := by
  obtain ⟨h1, h2⟩ := h
  unfold wake
  split
  · rename_i t ht
    split
    · refine ⟨h1, ?_⟩
      intro t' e r hr; cases e; simp at hr; exact hr ▸ hid
    · split
      · refine ⟨h1, ?_⟩
        intro t' e r hr; cases e
        rcases mem_addResume hr with rfl | hr
        · exact hid
        · exact h2 t ht r hr
      · exact ⟨h1, h2⟩
  · exact ⟨h1, h2⟩

theorem setSettled_id (s v now) : Doc.IdPres (setSettled s v now) := fun _ => rfl

theorem weak_setSettled {s v now} {o : Obj} (h : Weak o) (hs : s ≠ .pending) : Weak (setSettled s v now o) :=
  ⟨by simp [setSettled, hs], h.listeners, h.shape, h.target⟩

theorem refs_setSettled {ids s v now} {o : Obj} (h : Refs ids o) : Refs ids (setSettled s v now o) := h

theorem expire_id : Doc.IdPres expire := fun _ => rfl

theorem timeoutState_ne (p : Promise) : p.timeoutState ≠ .pending := by
  unfold Promise.timeoutState; split <;> simp

theorem weak_expire {o : Obj} (h : Weak o) : Weak (expire o) :=
  ⟨by simp [expire, timeoutState_ne], h.listeners, h.shape, h.target⟩

theorem expire_settled (o : Obj) : (expire o).promise.state ≠ .pending := timeoutState_ne _

end Objects

end Kernel
