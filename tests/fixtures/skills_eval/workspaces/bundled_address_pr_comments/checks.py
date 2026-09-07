from src.title import normalize_title

assert normalize_title("  Release Candidate  ") == "Release Candidate"
assert normalize_title("") == ""
