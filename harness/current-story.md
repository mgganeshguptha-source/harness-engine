# Story: Owner.hasPet(name)

**As** a clinic staff member
**I want** to check whether an owner already has a pet with a given name
**So that** I can prevent duplicate pet names when registering a new pet for that owner.

## Acceptance Criteria
1. Add a public method `boolean hasPet(String name)` to the `Owner` class.
2. Returns `true` if the owner has a pet whose name matches `name` (case-insensitive).
3. Returns `false` if no pet matches, or if `name` is null.
4. Does not modify existing pets or owner state (read-only).
5. Unit tests cover: a matching name, a non-matching name, case-insensitive match, and a null argument.


