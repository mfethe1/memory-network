from code_index.hashing import normalized_hash, raw_hash, short_uid


def test_raw_hash_detects_whitespace_changes():
    a = "def f():\n    return 1\n"
    b = "def  f():\n    return 1\n"
    assert raw_hash(a) != raw_hash(b)


def test_normalized_hash_stable_under_formatting():
    a = "def f():\n    return 1\n"
    b = "\n\ndef   f():\n        return  1\n\n\n"
    assert normalized_hash(a) == normalized_hash(b)


def test_normalized_hash_detects_real_change():
    a = "def f():\n    return 1\n"
    b = "def f():\n    return 2\n"
    assert normalized_hash(a) != normalized_hash(b)


def test_short_uid_deterministic():
    assert short_uid("foo") == short_uid("foo")
    assert short_uid("foo") != short_uid("bar")
