"""
test_boundaries.py — proves the core interlock BEFORE any SDK or credit spend.

Run:  python -m pytest harness/test_boundaries.py -v
(or:  python harness/test_boundaries.py   for a no-pytest fallback)
"""
from boundaries import is_write_allowed, _normalize
from phases import phase_by_id


# ---- the showpiece guarantees -------------------------------------------

def test_coding_phase_can_write_main():
    coding = phase_by_id("coding")
    assert is_write_allowed("src/main/java/org/.../Owner.java", coding.allowed_writes)


def test_coding_phase_CANNOT_write_tests():
    """THE headline interlock: during coding, test files are untouchable."""
    coding = phase_by_id("coding")
    assert not is_write_allowed("src/test/java/org/.../OwnerTests.java", coding.allowed_writes)


def test_testing_phase_can_write_tests():
    testing = phase_by_id("unit_testing")
    assert is_write_allowed("src/test/java/org/.../OwnerTests.java", testing.allowed_writes)


def test_testing_phase_CANNOT_write_main():
    """Mirror interlock: during testing, app code is frozen."""
    testing = phase_by_id("unit_testing")
    assert not is_write_allowed("src/main/java/org/.../Owner.java", testing.allowed_writes)


def test_context_phase_is_workspace_only():
    ctx = phase_by_id("context")
    assert is_write_allowed(".harness/context.md", ctx.allowed_writes)
    assert not is_write_allowed("src/main/java/X.java", ctx.allowed_writes)
    assert not is_write_allowed("src/test/java/X.java", ctx.allowed_writes)


# ---- defensive / escape cases -------------------------------------------

def test_path_escape_is_denied():
    coding = phase_by_id("coding")
    assert not is_write_allowed("../../../etc/passwd", coding.allowed_writes)
    assert not is_write_allowed("src/main/../../secret", coding.allowed_writes)


def test_windows_style_paths_normalize():
    coding = phase_by_id("coding")
    assert is_write_allowed("src\\main\\java\\Owner.java", coding.allowed_writes)
    assert not is_write_allowed("src\\test\\java\\OwnerTests.java", coding.allowed_writes)


def test_leading_dot_slash_normalizes():
    coding = phase_by_id("coding")
    assert is_write_allowed("./src/main/java/Owner.java", coding.allowed_writes)


def test_empty_globs_denies_everything():
    assert not is_write_allowed("anything.txt", ())


def test_docs_phase_scope():
    docs = phase_by_id("documentation")
    assert is_write_allowed("docs/api/fullname.md", docs.allowed_writes)
    assert not is_write_allowed("src/main/java/X.java", docs.allowed_writes)


# ---- absolute-path handling (the SDK reports absolute paths) ----
_ROOT = "C:/Users/x/projects/demo/spring-petclinic"

def test_abs_workspace_path_allowed_in_context():
    ctx = phase_by_id("context")
    p = r"C:\Users\x\projects\demo\spring-petclinic\.harness\context.md"
    assert is_write_allowed(p, ctx.allowed_writes, repo_root=_ROOT)

def test_abs_test_path_denied_in_coding():
    coding = phase_by_id("coding")
    p = r"C:\Users\x\projects\demo\spring-petclinic\src\test\java\X.java"
    assert not is_write_allowed(p, coding.allowed_writes, repo_root=_ROOT)

def test_abs_main_path_allowed_in_coding():
    coding = phase_by_id("coding")
    p = r"C:\Users\x\projects\demo\spring-petclinic\src\main\java\Owner.java"
    assert is_write_allowed(p, coding.allowed_writes, repo_root=_ROOT)

def test_abs_path_outside_repo_denied():
    ctx = phase_by_id("context")
    assert not is_write_allowed(r"C:\Windows\System32\evil.dll", ctx.allowed_writes, repo_root=_ROOT)


# ---- no-pytest fallback runner ------------------------------------------
if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}  {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
